#!/usr/bin/env python3
"""Prepare a private, reversible systemd context override; never restart the model."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile


STATE = Path('/var/lib/matchlab-qwen-context')
SYSTEM = Path('/etc/systemd/system')
DROPIN = '90-matchlab-context.conf'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError('Системная проверка недоступна; содержимое команды не выводится.') from error
    if result.returncode:
        raise ValueError('Системная проверка не прошла; её вывод скрыт, поскольку может содержать ключи.')
    return result.stdout


def unit_name(value):
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*\.service', value):
        raise ValueError('Нужно точное имя systemd-службы с окончанием .service.')
    return value


def properties(unit, call):
    output = call(['systemctl', 'show', '-p', 'FragmentPath', '-p', 'DropInPaths', '-p', 'LoadState', '--', unit])
    values = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    if values.get('LoadState') != 'loaded' or not values.get('FragmentPath'):
        raise ValueError('Служба не загружена или её unit-файл не найден.')
    return values


def private_write(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix='.qwen-context-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
        # link creates the complete file atomically and refuses an occupied name.
        os.link(temporary, path, follow_symlinks=False)
    finally:
        os.unlink(temporary)


def changed_start(source, target):
    """Handle a single literal ExecStart; refuse wrappers, variables and ambiguity."""
    text = source.decode('utf-8')
    starts = []
    section = ''; offset = 0; lines = iter(text.splitlines(keepends=True))
    for line in lines:
        begin = offset; offset += len(line)
        if line.strip().startswith('['):
            section = line.strip()
        if section != '[Service]' or not re.match(r'^\s*ExecStart\s*=', line):
            continue
        directive = line
        while directive.rstrip('\r\n').endswith('\\'):
            if directive.rstrip('\r\n').endswith('\\\\'):
                raise ValueError('Неоднозначное продолжение ExecStart; нужна ручная проверка.')
            extra = next(lines, '')
            if not extra:
                raise ValueError('Незавершённая директива ExecStart.')
            directive += extra; offset += len(extra)
        value = re.sub(r'^\s*ExecStart\s*=', '', directive)
        if value.strip():
            starts.append((begin, offset, directive, value))
    if len(starts) != 1:
        raise ValueError('Требуется один явный ExecStart в секции Service; unit не изменён.')
    begin, end, directive, value = starts[0]
    argv = shlex.split(re.sub(r'\\\r?\n', ' ', value))
    values = []
    for index, token in enumerate(argv):
        if token == '--max-model-len':
            values.append(argv[index + 1] if index + 1 < len(argv) else '')
        elif token.startswith('--max-model-len='):
            values.append(token.split('=', 1)[1])
    if len(values) != 1 or not values[0].isdigit():
        raise ValueError('В ExecStart нет одного числового --max-model-len; переменные и обёртки требуют ручной проверки.')
    pattern = r'''(?<![\w-])--max-model-len(?:[ \t]+|\\\r?\n|=)+["']?(?P<number>\d+)(?=["'\s\\]|$)'''
    matches = list(re.finditer(pattern, directive))
    if len(matches) != 1 or matches[0]['number'] != values[0]:
        raise ValueError('Нельзя однозначно изменить только --max-model-len; unit не изменён.')
    old = int(values[0])
    if not old < target <= 262144:
        raise ValueError('Новое окно должно быть больше текущего и не превышать 262144; поддержка и KV-кэш проверяются отдельно.')
    match = matches[0]
    changed = directive[:match.start('number')] + str(target) + directive[match.end('number'):]
    candidate = (text[:begin] + changed + text[end:]).encode()
    override = ('[Service]\nExecStart=\n' + changed.rstrip('\r\n') + '\n').encode()
    return old, candidate, override


def prepare(unit, target, call=run):
    unit_name(unit)
    values = properties(unit, call)
    if values.get('DropInPaths'):
        raise ValueError('У службы уже есть drop-in: требуется отдельная проверка приоритетов, файлы не изменены.')
    fragment = Path(values['FragmentPath'])
    if fragment.is_symlink() or not fragment.is_file():
        raise ValueError('Нужен обычный unit-файл, без символической ссылки.')
    source = fragment.read_bytes()
    old, candidate, override = changed_start(source, target)
    destination = SYSTEM / (unit + '.d') / DROPIN
    if destination.exists() or destination.is_symlink():
        raise ValueError('Путь drop-in уже занят; существующий файл не перезаписан.')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if STATE.is_symlink() or STATE.stat().st_uid != os.geteuid() or STATE.stat().st_mode & 0o077:
        raise ValueError('Каталог планов должен принадлежать текущему пользователю и иметь права 700.')
    folder = Path(tempfile.mkdtemp(prefix=unit + '-', dir=STATE))
    private_write(folder / 'original.service', source)
    (folder / 'candidate').mkdir(mode=0o700)
    private_write(folder / 'candidate' / unit, candidate)
    private_write(folder / 'dropin.conf', override)
    call(['systemd-analyze', 'verify', '--generators=no', '--man=no', str(folder / 'candidate' / unit)])
    manifest = {'format': 'matchlab.qwen-context.v1', 'unit': unit, 'fragment': str(fragment),
                'source_sha256': digest(source), 'override_sha256': digest(override), 'previous_tokens': old, 'target_tokens': target}
    private_write(folder / 'plan.json', json.dumps(manifest).encode())
    script = shlex.quote(str(Path(__file__).resolve())); plan = shlex.quote(str(folder / 'plan.json'))
    return {'state': 'prepared', 'unit': unit, 'changes': {'--max-model-len': [old, target]},
            'plan': str(folder / 'plan.json'), 'dropin': str(destination), 'restart_performed': False,
            'kv_capacity_verified': False, 'all_other_start_arguments_preserved': True,
            'apply_command': f'sudo python3 {script} apply --plan {plan}',
            'rollback_command': f'sudo python3 {script} rollback --plan {plan}'}


def load_plan(path):
    if path.name != 'plan.json' or path.parent.parent.resolve() != STATE.resolve():
        raise ValueError('Нужен plan.json из закрытого каталога, созданного командой prepare.')
    for folder in (STATE, path.parent):
        if folder.is_symlink() or folder.stat().st_uid != os.geteuid() or folder.stat().st_mode & 0o077:
            raise ValueError('Каталог плана должен принадлежать текущему пользователю и иметь права 700.')
    for item in (path, path.parent / 'dropin.conf'):
        if item.is_symlink() or not item.is_file() or item.stat().st_uid != os.geteuid() or item.stat().st_mode & 0o077:
            raise ValueError('Файлы плана должны быть обычными закрытыми файлами текущего пользователя.')
    manifest = json.loads(path.read_bytes())
    if manifest.get('format') != 'matchlab.qwen-context.v1':
        raise ValueError('Неизвестный формат плана.')
    unit_name(manifest['unit'])
    override = (path.parent / 'dropin.conf').read_bytes()
    if digest(override) != manifest['override_sha256']:
        raise ValueError('Подготовленный drop-in изменён; действие остановлено.')
    return manifest, override


def apply(path, call=run):
    plan, override = load_plan(path)
    unit = plan['unit']; values = properties(unit, call)
    if values.get('DropInPaths') or values['FragmentPath'] != plan['fragment']:
        raise ValueError('Конфигурация службы изменилась после подготовки; действие остановлено.')
    fragment = Path(plan['fragment'])
    if fragment.is_symlink() or digest(fragment.read_bytes()) != plan['source_sha256']:
        raise ValueError('Исходный unit изменился после подготовки; действие остановлено.')
    destination = SYSTEM / (unit + '.d') / DROPIN
    if destination.parent.is_symlink():
        raise ValueError('Каталог drop-in не должен быть символической ссылкой.')
    destination.parent.mkdir(mode=0o755, exist_ok=True)
    private_write(destination, override)
    try:
        call(['systemctl', 'daemon-reload'])
    except ValueError:
        destination.unlink()
        call(['systemctl', 'daemon-reload'])
        raise
    return {'state': 'installed', 'unit': unit, 'restart_required': True, 'restart_performed': False}


def rollback(path, call=run):
    plan, override = load_plan(path)
    destination = SYSTEM / (plan['unit'] + '.d') / DROPIN
    if destination.parent.is_symlink() or destination.is_symlink():
        raise ValueError('Путь drop-in изменён; откат остановлен.')
    if destination.exists():
        if destination.read_bytes() != override:
            raise ValueError('Drop-in изменён вручную; чужие изменения не удалены.')
        destination.unlink()
    call(['systemctl', 'daemon-reload'])
    return {'state': 'override_removed', 'unit': plan['unit'], 'restart_required': True, 'restart_performed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    first = sub.add_parser('prepare'); first.add_argument('--unit', default='vllm.service', type=unit_name)
    first.add_argument('--tokens', type=int, default=131072)
    for command in ('apply', 'rollback'):
        sub.add_parser(command).add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.exit(1, 'Запустите через sudo: планы могут содержать закрытые параметры unit-файла.\n')
    os.umask(0o077)
    try:
        result = prepare(args.unit, args.tokens) if args.command == 'prepare' else globals()[args.command](args.plan)
    except (OSError, ValueError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, ValueError) and not isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)) else 'Не удалось выполнить действие; содержимое unit-файла и ключи не выводятся.'
        parser.exit(1, message + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
