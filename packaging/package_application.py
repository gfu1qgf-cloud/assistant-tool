"""Stream a Windows application directory into a new ZIP without Update-mode RAM spikes."""
import argparse
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED


def package(application, output):
    application, output = Path(application), Path(output)
    if application.is_symlink() or getattr(application, 'is_junction', lambda: False)():
        raise ValueError('Application directory cannot be a link')
    root = application.resolve(strict=True)
    target = output.absolute()
    if target.resolve().is_relative_to(root):
        raise ValueError('Output cannot be inside the application directory')
    if not (root/'AssistantTool.exe').is_file() or not (root/'BUILD_INFO.json').is_file():
        raise ValueError('Application executable or build identity is missing')
    paths = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('Application resources cannot be links')
        if path.is_file():
            if not path.resolve().is_relative_to(root):
                raise ValueError('Resource escapes application directory')
            paths.append(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation ensures that cleanup can only remove our own partial
    # output. ZipFile.write streams each file; never use ZipArchive Update mode.
    with target.open('xb') as stream:
        try:
            with ZipFile(stream, 'w', compression=ZIP_DEFLATED, compresslevel=6,
                         allowZip64=True) as archive:
                for path in paths:
                    archive.write(path, path.relative_to(root).as_posix())
        except BaseException:
            stream.close()
            target.unlink()
            raise
    return len(paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    count = package(args.application, args.output)
    print(f'Application ZIP ready: {count} files; {args.output.stat().st_size} bytes')


if __name__ == '__main__':
    main()
