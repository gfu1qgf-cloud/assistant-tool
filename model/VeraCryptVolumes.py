"""Read-only Windows VeraCrypt 1.26.7 driver adapter; never mount/dismount.

ABI: upstream VeraCrypt_1.26.7 Common/Apidrvr.h, Common.h and Tcdefs.h.
Use explicit Windows-width integers/UTF-16, not platform-native c_long/wchar.
Unknown driver patch versions fail closed until their ABI has been verified.
"""
import ctypes as C
from dataclasses import dataclass
import os
from pathlib import Path
import re
import struct
import threading


class VolumeError(RuntimeError):
    pass


U16 = C.c_uint16
I32 = C.c_int32
U32 = C.c_uint32
U64 = C.c_uint64


class MountList(C.LittleEndianStructure):
    _pack_ = 1
    _fields_ = [('mask', U32), ('paths', (U16 * 260) * 26), ('labels', (U16 * 33) * 26),
                ('ids', (U16 * 32) * 26), ('lengths', U64 * 26), ('algorithms', I32 * 26),
                ('types', I32 * 26), ('reserved', I32 * 26)]


class Properties(C.LittleEndianStructure):
    _pack_ = 1
    _fields_ = [('drive', I32), ('unique_id', I32), ('path', U16 * 260), ('length', U64),
        ('algorithm', I32), ('mode', I32), ('pkcs5', I32), ('iterations', I32),
        ('hidden', I32), ('readonly', I32), ('removable', I32), ('partition', I32),
        ('flags', U32), ('read_bytes', U64), ('write_bytes', U64), ('hidden_protection', I32),
        ('format_version', I32), ('pim', I32), ('label', U16 * 33), ('driver_label', I32),
        ('volume_id', C.c_ubyte * 32), ('mount_disabled', I32)]


def ioctl_code(number):
    return (0x22 << 16) | ((0x800 + number) << 2)


def wide(value):
    return bytes(value).decode('utf-16-le').split('\0', 1)[0]


@dataclass(frozen=True)
class MountedVolume:
    root: str
    volume_id: str
    unique_id: int
    device: str
    length: int
    container: str = ''
    readonly: bool = False


class WindowsDriver:
    def __init__(self):
        if os.name != 'nt':
            raise VolumeError('VC 强制模式目前只支持 Windows。')
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateFileW.argtypes = [C.c_wchar_p, U32, U32, C.c_void_p, U32, U32, C.c_void_p]
        self.kernel.CreateFileW.restype = C.c_void_p
        self.kernel.CloseHandle.argtypes = [C.c_void_p]
        self.kernel.CloseHandle.restype = I32
        self.kernel.DeviceIoControl.argtypes = [C.c_void_p, U32, C.c_void_p, U32,
                                                C.c_void_p, U32, C.POINTER(U32), C.c_void_p]
        self.kernel.DeviceIoControl.restype = I32
        self.kernel.QueryDosDeviceW.argtypes = [C.c_wchar_p, C.c_wchar_p, U32]
        self.kernel.QueryDosDeviceW.restype = U32
        self.kernel.GetFinalPathNameByHandleW.argtypes = [C.c_void_p, C.c_wchar_p, U32, U32]
        self.kernel.GetFinalPathNameByHandleW.restype = U32

    def open_handle(self, path, *, directory=False, shared_write=True):
        # No password API; no write or dismount IOCTL is exposed.
        handle = self.kernel.CreateFileW(str(path), 0x80000000, 3 if shared_write else 1, None, 3,
            0x02000000 if directory else 0, None)
        if handle == C.c_void_p(-1).value:
            raise VolumeError('无法访问 VC 驱动或加密数据目录。请先在 VeraCrypt 中挂载卷。')
        return handle

    def close(self, handle):
        self.kernel.CloseHandle(handle)

    def request(self, handle, number, size, initial=b''):
        output = C.create_string_buffer(size)
        incoming = C.create_string_buffer(initial) if initial else None
        returned = U32()
        if not self.kernel.DeviceIoControl(handle, ioctl_code(number), incoming,
                len(initial), output, size, C.byref(returned), None):
            raise VolumeError('VC 驱动查询失败；未将任何普通磁盘当作加密卷。')
        if returned.value != size:
            raise VolumeError('VC 驱动返回结构长度不兼容，请勿继续使用该数据位置。')
        return output.raw

    def device(self, letter):
        buffer = C.create_unicode_buffer(32768)
        if not self.kernel.QueryDosDeviceW(letter, buffer, len(buffer)):
            raise VolumeError('无法确认盘符实际设备映射。')
        return buffer.value

    def version(self):
        # Driver VERSION_NUM is only 0x0126; additionally verify the file patch.
        version = C.WinDLL('version', use_last_error=True)
        version.GetFileVersionInfoSizeW.argtypes = [C.c_wchar_p, C.POINTER(U32)]
        version.GetFileVersionInfoSizeW.restype = U32
        version.GetFileVersionInfoW.argtypes = [C.c_wchar_p, U32, U32, C.c_void_p]
        version.VerQueryValueW.argtypes = [C.c_void_p, C.c_wchar_p, C.POINTER(C.c_void_p), C.POINTER(U32)]
        path = str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/drivers/veracrypt.sys')
        size = version.GetFileVersionInfoSizeW(path, C.byref(U32()))
        if not size:
            raise VolumeError('无法确认 VC 驱动补丁版本；仅支持已核对的 1.26.7。')
        buffer = C.create_string_buffer(size)
        pointer, length = C.c_void_p(), U32()
        if not version.GetFileVersionInfoW(path, 0, size, buffer) or not version.VerQueryValueW(
                buffer, '\\', C.byref(pointer), C.byref(length)) or length.value < 52:
            raise VolumeError('无法读取 VC 驱动版本。')
        words = struct.unpack('<13I', C.string_at(pointer.value, 52))
        ms, ls = words[2:4]
        return (ms >> 16, ms & 65535, ls >> 16, ls & 65535)

    def final_device_path(self, handle):
        buffer = C.create_unicode_buffer(32768)
        count = self.kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 2)  # VOLUME_NAME_NT
        if not count or count >= len(buffer):
            raise VolumeError('无法确认打开的数据目录仍属于选定的 VC 卷。')
        return buffer.value


class VeraCryptVolumes:
    def __init__(self, driver=None):
        self.driver = driver or WindowsDriver()

    def mounted(self):
        driver = self.driver
        if driver.version() != (1, 26, 7, 0):
            raise VolumeError('当前 VC 驱动补丁版本尚未验证，拒绝启用加密数据位置。支持版本：1.26.7。')
        handle = driver.open_handle(r'\\.\VeraCrypt')
        try:
            if struct.unpack('<i', driver.request(handle, 1, 4))[0] != 0x126:
                raise VolumeError('VC 驱动版本不兼容。')
            record = MountList.from_buffer_copy(driver.request(handle, 6, C.sizeof(MountList)))
            if record.mask >> 26:
                raise VolumeError('VC 挂载清单无效。')
            volumes = []
            for number in range(26):
                if not record.mask & (1 << number):
                    continue
                request = Properties(); request.drive = number
                props = Properties.from_buffer_copy(driver.request(handle, 7, C.sizeof(Properties), bytes(request)))
                letter = chr(65 + number) + ':'
                device = driver.device(letter)
                # SUBST, network drives and labels cannot pass this check.
                if device.casefold() != f'\\Device\\VeraCryptVolume{chr(65+number)}'.casefold():
                    raise VolumeError('盘符不是驱动确认的 VC 设备，拒绝使用。')
                identity = bytes(props.volume_id)
                if (props.drive != number or not any(identity) or props.length != record.lengths[number]
                        or identity != bytes(record.ids[number])[:32] or props.mount_disabled
                        or props.hidden_protection == 2):
                    raise VolumeError('VC 卷属性与挂载清单不一致或已禁止写入。')
                volumes.append(MountedVolume(letter + '\\', identity.hex(), props.unique_id, device,
                    props.length, wide(props.path), bool(props.readonly)))
            return volumes
        finally:
            driver.close(handle)

    def verify(self, root, *, identity=None, unique_id=None):
        if not re.fullmatch(r'[A-Za-z]:[\\/]?', str(root)):
            raise VolumeError('请选择已验证的 VC 挂载盘符；不接受网络位置或普通目录。')
        letter = str(root)[:2].upper()
        found = next((value for value in self.mounted() if value.root[:2] == letter), None)
        if found is None:
            raise VolumeError('所选 VC 卷尚未挂载或当前用户无法访问。请先挂载后刷新。')
        if found.readonly or identity and found.volume_id != identity or unique_id is not None and found.unique_id != unique_id:
            raise VolumeError('VC 卷只读、身份改变或已重新挂载；停止私有数据读写，请重启并重新选择。')
        return found

    def open_verifier(self, volume):
        return BoundVolumeVerifier(self.driver, volume)


class BoundVolumeVerifier:
    """Live identity queries on one retained, version-verified driver handle.

    No positive-result TTL: every operation checks the selected device and
    mount generation. Retaining the handle avoids reopening/version-file I/O
    and the unrelated mounted volumes are not enumerated for every byte.
    """
    def __init__(self, driver, volume):
        self.driver, self.volume = driver, volume
        self.lock, self.handle = threading.RLock(), None
        if driver.version() != (1, 26, 7, 0):
            raise VolumeError('当前 VC 驱动补丁版本尚未验证，拒绝启用加密数据位置。支持版本：1.26.7。')
        if not re.fullmatch(r'[A-Za-z]:[\\/]?', str(volume.root)):
            raise VolumeError('VC 会话需要已验证的挂载盘符。')
        self.number = ord(volume.root[0].upper())-65
        if (volume.device.casefold() != f'\\Device\\VeraCryptVolume{chr(65+self.number)}'.casefold()
                or not re.fullmatch('[0-9a-f]{64}',volume.volume_id) or not any(bytes.fromhex(volume.volume_id))):
            raise VolumeError('VC 会话的设备或卷身份无效。')
        self.handle = driver.open_handle(r'\\.\VeraCrypt')
        try:
            if struct.unpack('<i',driver.request(self.handle,1,4))[0] != 0x126:
                raise VolumeError('VC 驱动版本不兼容。')
            self.check()
        except BaseException:
            self.close(); raise

    def check(self):
        with self.lock:
            if self.handle is None:
                raise VolumeError('VC 驱动验证句柄已关闭，停止私有数据读写。')
            expected = self.volume
            # Query the letter as well as the driver property: a SUBST or
            # reused normal drive must never inherit this session's approval.
            device = self.driver.device(expected.root[:2])
            if device.casefold() != expected.device.casefold():
                raise VolumeError('VC 盘符的实际设备映射发生改变。')
            request=Properties();request.drive=self.number
            props=Properties.from_buffer_copy(self.driver.request(self.handle,7,C.sizeof(Properties),bytes(request)))
            if (props.drive != self.number or bytes(props.volume_id).hex() != expected.volume_id
                    or props.unique_id != expected.unique_id or props.length != expected.length
                    or props.readonly or props.mount_disabled or props.hidden_protection == 2):
                raise VolumeError('VC 卷身份、挂载代次或可写状态发生改变；请重启并重新选择卷。')
            return expected

    def close(self):
        with self.lock:
            if self.handle is not None:
                self.driver.close(self.handle);self.handle=None
