"""Build a portable distribution from an explicit list of public files.

Place the portable runtime in runtime/ before running this script. Runtime
provenance is recorded in runtime-components.json; no local game data is read.
"""
import hashlib
import json
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not (ROOT / 'runtime/python/python.exe').is_file():
        raise SystemExit('请先将完整试玩包中的 runtime 文件夹放到项目根目录。')
    version = re.search(r'^version = "([^"]+)"',
                        (ROOT / 'pyproject.toml').read_text(encoding='utf-8'), re.M).group(1)
    output = ROOT / 'releases' / f'三五反远程试玩包_v{version}.zip'
    output.parent.mkdir(exist_ok=True)
    if output.exists():
        raise SystemExit(f'已有发布包，未覆盖：{output}')
    contents = {}
    for directory in ('sanwufan', 'tests', 'runtime', 'docs'):
        for path in (ROOT / directory).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                contents[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for name in ('README.md', 'pyproject.toml', 'requirements-server.txt',
                 'requirements-remote.txt', 'Dockerfile', 'compose.yaml',
                 '.dockerignore', '.gitignore', '.env.example',
                 'deploy/README.md', 'deploy/Caddyfile',
                 'scripts/package_remote.py', 'scripts/runtime-components.json'):
        contents[name] = (ROOT / name).read_bytes()
    for name in ('启动牌桌.cmd', '启动远程试玩.cmd'):
        contents[name] = (ROOT / name).read_text(encoding='utf-8-sig').replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-8')
    contents['组件来源.json'] = (ROOT / 'scripts/runtime-components.json').read_bytes()
    contents['MANIFEST.sha256'] = ''.join(
        f'{hashlib.sha256(data).hexdigest()}  {name}\n'
        for name, data in sorted(contents.items())).encode('utf-8')
    assert not any(name.startswith('data/') or '.sqlite' in name for name in contents)
    with ZipFile(output, 'x', ZIP_DEFLATED, compresslevel=9) as bundle:
        for name, data in contents.items():
            bundle.writestr('三五反远程试玩包/' + name, data)
        assert bundle.testzip() is None
    print(json.dumps({'archive': str(output), 'files': len(contents),
                      'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
