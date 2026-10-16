"""SDR Rec.709 working space for compositing SDR graphics over video.

HDR backgrounds must be tone-mapped BEFORE adding text/images. Merely changing
the final tags would reinterpret HDR samples; keeping the HDR tags would make
the SDR artwork HDR. Preview and encoding share the same native FFmpeg filters.
"""

SDR_TAGS = 'setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709'


def sdr_input_filter(stream):
    stream = stream or {}
    transfer = stream.get('color_transfer', '')
    primaries = stream.get('color_primaries', '')
    hdr = transfer in {'arib-std-b67', 'smpte2084'}
    wide = primaries in {'bt2020', 'smpte432'}
    if not hdr and not wide:
        return ''
    allowed_transfers = {'arib-std-b67', 'smpte2084', 'bt709', 'bt2020-10', 'bt2020-12', 'iec61966-2-1'}
    if transfer not in allowed_transfers:
        raise ValueError('宽色域背景缺少可识别的传递函数，无法安全合成SDR图片；请先转换为Rec.709。')
    primary = primaries if primaries in {'bt2020', 'bt709', 'smpte432'} else 'bt2020'
    matrix = stream.get('color_space')
    if matrix not in {'bt2020nc', 'bt2020c', 'bt709', 'gbr'}:
        matrix = 'bt2020nc' if primary == 'bt2020' else 'bt709'
    color_range = 'full' if stream.get('color_range') == 'pc' else 'limited'
    initial = f'zscale=primariesin={primary}:transferin={transfer}:matrixin={matrix}:rangein={color_range}'
    if hdr:
        # FFmpeg tonemap operates on linear floating-point samples. npl is the
        # SDR reference white, not a guessed source peak; tonemap reads the peak.
        result = (initial + ':transfer=linear:npl=100,format=gbrpf32le,'
            'zscale=primaries=bt709,tonemap=tonemap=mobius:desat=2,'
            'zscale=transfer=bt709:matrix=bt709:range=limited,format=yuv420p')
    else:
        result = initial + ':primaries=bt709:transfer=bt709:matrix=bt709:range=limited,format=yuv420p'
    return (result + ',sidedata=mode=delete:type=MASTERING_DISPLAY_METADATA,'
        'sidedata=mode=delete:type=CONTENT_LIGHT_LEVEL,' + SDR_TAGS)


def sdr_output_filter():
    # Specify the matrix conversion as well as the metadata. RGB picture pixels
    # must be encoded with the same matrix the player will use to decode them.
    return 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,' + SDR_TAGS


def sdr_output_args():
    return ['-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-color_range', 'tv']
