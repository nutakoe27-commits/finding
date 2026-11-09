#!/usr/bin/env python3
"""Prepare reversible context changes for a unit or its launch script; never restart."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile


STATE = Path('/var/lib/matchlab-qwen-context')
SYSTEM = Path('/etc/systemd/system')
DROPIN = '90-matchlab-context.conf'
CONTEXT_NUMBER = r'''(?<![\w-])--max-model-len(?:[ \t]+|\\\r?\n|=)+["']?(?P<number>\d+)(?=["'\s\\]|$)'''


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


def command_shape(command, depth=0):
    """Report structure, never unselected argument or environment values."""
    variables = sorted(set(re.findall(r'\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))', command)))
    result = {'variable_names': sorted({a or b for a, b in variables})}
    try:
        argv = shlex.split(re.sub(r'\\\r?\n', ' ', command), comments=True)
    except ValueError:
        return result | {'parse': 'requires_manual_review'}
    if not argv:
        return result | {'parse': 'empty'}
    program = argv[0].lstrip('-+!@:')
    if re.fullmatch(r'[A-Za-z0-9_./@%+:-]+', program):
        result['program'] = program
    else:
        result['program'] = 'expression'
    known_options = {'--model', '--served-model-name', '--max-model-len', '--tensor-parallel-size', '--quantization',
                     '--dtype', '--gpu-memory-utilization', '--kv-cache-dtype', '--max-num-seqs', '--api-key',
                     '--port', '--host', '--config', '--enable-chunked-prefill', '--max-num-batched-tokens'}
    result['option_names'] = sorted({token.split('=', 1)[0] for token in argv if token.split('=', 1)[0] in known_options})
    if program.startswith('/') and re.fullmatch(r'/[A-Za-z0-9_./@%+-]+\.sh', program):
        result['launch_script'] = program
    lengths = []
    for index, token in enumerate(argv):
        value = None
        if token == '--max-model-len':
            value = argv[index + 1] if index + 1 < len(argv) else ''
        elif token.startswith('--max-model-len='):
            value = token.split('=', 1)[1]
        if value is not None:
            if re.fullmatch(r'[0-9]{1,9}', value):
                lengths.append({'kind': 'literal', 'tokens': int(value)})
            elif re.fullmatch(r'\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})', value):
                lengths.append({'kind': 'variable', 'name': value.strip('${}')})
            else:
                lengths.append({'kind': 'expression'})
    result['context_arguments'] = lengths
    if depth < 2 and Path(program).name in ('sh', 'bash', 'dash', 'zsh'):
        for index, token in enumerate(argv[1:], 1):
            if re.fullmatch(r'-[A-Za-z]*c[A-Za-z]*', token) and index + 1 < len(argv):
                result['shell_command'] = command_shape(argv[index + 1], depth + 1)
                break
        else:
            if len(argv) > 1 and re.fullmatch(r'/[A-Za-z0-9_./@%+-]+\.sh', argv[1]):
                result['launch_script'] = argv[1]
    return result


def inspect(unit, call=run):
    """Read the unit and referenced absolute shell scripts; no system mutations."""
    values = properties(unit_name(unit), call)
    files = [Path(values['FragmentPath'])] + [Path(p) for p in shlex.split(values.get('DropInPaths', ''))]
    result = {'state': 'inspection_only', 'unit': unit, 'restart_performed': False, 'files': []}
    scripts = set()
    for path in files:
        text = re.sub(r'\\\r?\n', ' ', path.read_text())
        summary = {'path': str(path), 'environment_names': [], 'environment_files': [], 'starts': []}
        section = ''
        for line in text.splitlines():
            if line.strip().startswith('['):
                section = line.strip()
            if section != '[Service]' or '=' not in line or line.lstrip().startswith(('#', ';')):
                continue
            key, value = line.strip().split('=', 1); key = key.strip()
            if key in ('Environment', 'EnvironmentFile'):
                try: items = shlex.split(value)
                except ValueError: continue
                if key == 'Environment':
                    summary['environment_names'] += [item.split('=', 1)[0] for item in items if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', item, re.S)]
                else:
                    summary['environment_files'] += [item for item in items if re.fullmatch(r'-?/[A-Za-z0-9_./@%+:-]+', item)]
            elif key == 'ExecStart' and value.strip():
                shape = command_shape(value); summary['starts'].append(shape)
                # Read only the executable script or bash's explicit script operand.
                while shape:
                    if shape.get('launch_script'):
                        scripts.add(Path(shape['launch_script']))
                    shape = shape.get('shell_command')
        summary['environment_names'] = sorted(set(summary['environment_names']))
        result['files'].append(summary)
    result['launch_scripts'] = []
    for path in sorted(scripts)[:4]:
        item = {'path': str(path)}
        try:
            if not path.is_file() or path.stat().st_size > 65536:
                item['read'] = 'unavailable_or_too_large'
            else:
                item['shape'] = command_shape(path.read_text())
        except (OSError, UnicodeDecodeError):
            item['read'] = 'unavailable'
        result['launch_scripts'].append(item)
    return result


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
    matches = list(re.finditer(CONTEXT_NUMBER, directive))
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


def prepare(unit, target, call=run, launch_script=None):
    if launch_script is not None:
        return prepare_script(unit, target, launch_script, call)
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
    check_private_file(path)
    manifest = json.loads(path.read_bytes())
    formats = {'matchlab.qwen-context.v1': ('dropin.conf', 'override_sha256'),
               'matchlab.qwen-script-context.v1': ('candidate.sh', 'candidate_sha256')}
    if manifest.get('format') not in formats:
        raise ValueError('Неизвестный формат плана.')
    unit_name(manifest['unit'])
    artifact, checksum = formats[manifest['format']]
    check_private_file(path.parent / artifact)
    override = (path.parent / artifact).read_bytes()
    if digest(override) != manifest[checksum]:
        raise ValueError('Подготовленный файл изменён; действие остановлено.')
    return manifest, override


def apply(path, call=run):
    plan, override = load_plan(path)
    if plan['format'] == 'matchlab.qwen-script-context.v1':
        return apply_script(plan, override, call)
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
    if plan['format'] == 'matchlab.qwen-script-context.v1':
        check_private_file(path.parent / 'original.sh')
        original = (path.parent / 'original.sh').read_bytes()
        if digest(original) != plan['script_sha256']:
            raise ValueError('Резервная копия скрипта изменена; откат остановлен.')
        script = Path(plan['launch_script'])
        if not script.is_symlink() and script.is_file() and script.read_bytes() == original:
            return {'state': 'script_already_restored', 'unit': plan['unit'], 'restart_required': True, 'restart_performed': False}
        replace_script(script, plan['candidate_sha256'], original, plan['metadata'])
        return {'state': 'script_restored', 'unit': plan['unit'], 'restart_required': True, 'restart_performed': False}
    destination = SYSTEM / (plan['unit'] + '.d') / DROPIN
    if destination.parent.is_symlink() or destination.is_symlink():
        raise ValueError('Путь drop-in изменён; откат остановлен.')
    if destination.exists():
        if destination.read_bytes() != override:
            raise ValueError('Drop-in изменён вручную; чужие изменения не удалены.')
        destination.unlink()
    call(['systemctl', 'daemon-reload'])
    return {'state': 'override_removed', 'unit': plan['unit'], 'restart_required': True, 'restart_performed': False}


def check_private_file(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077:
        raise ValueError('Файлы плана должны быть обычными закрытыми файлами текущего пользователя.')


def script_binding(source, script):
    """Only support a unit directly executing this exact, argument-free script."""
    section = ''; starts = []
    for line in re.sub(r'\\\r?\n', ' ', source.decode('utf-8')).splitlines():
        if line.strip().startswith('['): section = line.strip()
        if section == '[Service]' and re.match(r'^\s*ExecStart\s*=', line):
            value = line.split('=', 1)[1].strip()
            if value: starts.append(shlex.split(value))
    if len(starts) != 1 or len(starts[0]) != 1 or starts[0][0].lstrip('-+!@:') != str(script):
        raise ValueError('Unit должен напрямую запускать указанный скрипт без дополнительных аргументов.')


def script_metadata(script):
    info = script.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Нужен обычный скрипт без символических и жёстких ссылок.')
    return {'uid': info.st_uid, 'gid': info.st_gid, 'mode': stat.S_IMODE(info.st_mode)}


def prepare_script(unit, target, script, call=run):
    unit_name(unit); script = Path(script)
    if not script.is_absolute() or script.suffix != '.sh':
        raise ValueError('Нужен абсолютный путь к запускаемому .sh-файлу.')
    values = properties(unit, call)
    if values.get('DropInPaths'):
        raise ValueError('У службы есть drop-in: требуется отдельная проверка приоритетов.')
    fragment = Path(values['FragmentPath'])
    if fragment.is_symlink() or not fragment.is_file():
        raise ValueError('Нужен обычный unit-файл, без символической ссылки.')
    unit_source = fragment.read_bytes(); script_binding(unit_source, script)
    metadata = script_metadata(script); source = script.read_bytes()
    if len(source) > 65536 or not source.startswith(b'#!'):
        raise ValueError('Требуется shell-скрипт с shebang размером не более 64 КБ.')
    text = source.decode('utf-8'); arguments = command_shape(text).get('context_arguments', [])
    matches = list(re.finditer(CONTEXT_NUMBER, text))
    if len(arguments) != 1 or arguments[0].get('kind') != 'literal' or len(matches) != 1:
        raise ValueError('В скрипте требуется один однозначный числовой --max-model-len.')
    old = arguments[0]['tokens']; match = matches[0]
    if int(match['number']) != old or not old < target <= 262144:
        raise ValueError('Нельзя однозначно увеличить окно скрипта до указанного значения.')
    candidate = (text[:match.start('number')] + str(target) + text[match.end('number'):]).encode()
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if STATE.is_symlink() or STATE.stat().st_uid != os.geteuid() or STATE.stat().st_mode & 0o077:
        raise ValueError('Каталог планов должен принадлежать текущему пользователю и иметь права 700.')
    folder = Path(tempfile.mkdtemp(prefix=unit + '-script-', dir=STATE))
    private_write(folder / 'original.service', unit_source)
    private_write(folder / 'original.sh', source); private_write(folder / 'candidate.sh', candidate)
    call(['env', '-u', 'BASH_ENV', '-u', 'ENV', 'bash', '--noprofile', '--norc', '-n', str(folder / 'candidate.sh')])
    manifest = {'format': 'matchlab.qwen-script-context.v1', 'unit': unit, 'fragment': str(fragment),
                'source_sha256': digest(unit_source), 'launch_script': str(script), 'script_sha256': digest(source),
                'candidate_sha256': digest(candidate), 'metadata': metadata, 'previous_tokens': old, 'target_tokens': target}
    private_write(folder / 'plan.json', json.dumps(manifest).encode())
    executable = shlex.quote(str(Path(__file__).resolve())); plan = shlex.quote(str(folder / 'plan.json'))
    return {'state': 'prepared', 'change_kind': 'launch_script', 'unit': unit, 'launch_script': str(script),
            'changes': {'--max-model-len': [old, target]}, 'plan': str(folder / 'plan.json'),
            'restart_performed': False, 'kv_capacity_verified': False, 'all_other_script_bytes_preserved': True,
            'apply_command': f'sudo python3 {executable} apply --plan {plan}',
            'rollback_command': f'sudo python3 {executable} rollback --plan {plan}'}


def replace_script(script, expected_hash, replacement, metadata):
    if script_metadata(script) != metadata or digest(script.read_bytes()) != expected_hash:
        raise ValueError('Скрипт, его владелец или права изменились; действие остановлено.')
    descriptor, temporary = tempfile.mkstemp(prefix='.matchlab-context-', dir=script.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(replacement); output.flush(); os.fsync(output.fileno())
            os.fchown(output.fileno(), metadata['uid'], metadata['gid'])
            os.fchmod(output.fileno(), metadata['mode'])
        shutil.copystat(script, temporary, follow_symlinks=False)
        if script_metadata(script) != metadata or digest(script.read_bytes()) != expected_hash:
            raise ValueError('Скрипт изменился во время подготовки записи; действие остановлено.')
        os.replace(temporary, script)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def apply_script(plan, candidate, call):
    values = properties(plan['unit'], call)
    if values.get('DropInPaths') or values['FragmentPath'] != plan['fragment']:
        raise ValueError('Конфигурация службы изменилась после подготовки; действие остановлено.')
    fragment = Path(plan['fragment'])
    if fragment.is_symlink() or digest(fragment.read_bytes()) != plan['source_sha256']:
        raise ValueError('Исходный unit изменился после подготовки; действие остановлено.')
    script = Path(plan['launch_script']); script_binding(fragment.read_bytes(), script)
    replace_script(script, plan['script_sha256'], candidate, plan['metadata'])
    return {'state': 'script_updated', 'unit': plan['unit'], 'launch_script': str(script),
            'restart_required': True, 'restart_performed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    first = sub.add_parser('prepare'); first.add_argument('--unit', default='vllm.service', type=unit_name)
    first.add_argument('--tokens', type=int, default=131072)
    first.add_argument('--launch-script', type=Path, help='Exact absolute .sh path executed directly by this unit')
    sub.add_parser('inspect').add_argument('--unit', default='vllm.service', type=unit_name)
    for command in ('apply', 'rollback'):
        sub.add_parser(command).add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.exit(1, 'Запустите через sudo: планы могут содержать закрытые параметры unit-файла.\n')
    os.umask(0o077)
    try:
        if args.command == 'prepare':
            result = prepare(args.unit, args.tokens, launch_script=args.launch_script)
        elif args.command == 'inspect':
            result = inspect(args.unit)
        else:
            result = globals()[args.command](args.plan)
    except (OSError, ValueError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, ValueError) and not isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)) else 'Не удалось выполнить действие; содержимое unit-файла и ключи не выводятся.'
        parser.exit(1, message + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
