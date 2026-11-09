#!/usr/bin/env python3
"""Update only the selected launcher options, with a private backup; never restart."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import tempfile

if __package__:
    from . import configure_qwen_context as files
else:
    import configure_qwen_context as files


BACKUPS = Path('/var/backups')
PROFILES = {
    '128k': {'--max-model-len': '131072', '--gpu-memory-utilization': '0.60',
             '--max-num-batched-tokens': '2048', '--max-num-seqs': '2'},
    '32k': {'--max-model-len': '32768', '--gpu-memory-utilization': '0.82',
            '--max-num-batched-tokens': '8192', '--max-num-seqs': '4'},
}


def tokens(text):
    return shlex.split(re.sub(r'\\\r?\n', ' ', text), comments=True)


def numeric_pattern(flag):
    return (r'(?<![\w-])' + re.escape(flag) +
            r'''(?:[ \t]+|\\\r?\n|=)+(?P<quote>["']?)(?P<value>\d+(?:\.\d+)?)(?P=quote)(?=[\s\\]|$)''')


def numeric(text, flag, value):
    argv = tokens(text); values = []
    for index, token in enumerate(argv):
        if token == flag:
            values.append(argv[index + 1] if index + 1 < len(argv) else '')
        elif token.startswith(flag + '='):
            values.append(token.split('=', 1)[1])
    matches = list(re.finditer(numeric_pattern(flag), text))
    if len(values) != 1 or len(matches) != 1 or matches[0]['value'] != values[0]:
        raise ValueError('Требуется одно явное числовое значение ' + flag + '; файл не изменён.')
    match = matches[0]
    return text[:match.start('value')] + value + text[match.end('value'):]


def boolean(text, flag, enabled):
    argv = tokens(text)
    if '--no-' + flag[2:] in argv:
        raise ValueError('Найден отрицательный флаг для ' + flag + '; требуется ручная проверка.')
    active = sum(token == flag or token.startswith(flag + '=') for token in argv)
    pattern = (r'(?<![\w-])' + re.escape(flag) +
               r'(?:=(?:true|false)|[ \t]+(?:true|false))?(?=[\s\\]|$)')
    matches = list(re.finditer(pattern, text, re.I))
    if active != len(matches) or active > 1:
        raise ValueError('Неоднозначный флаг ' + flag + '; файл не изменён.')
    if active:
        match = matches[0]
        return text[:match.start()] + (flag if enabled else '') + text[match.end():]
    if enabled:
        anchor = re.search(numeric_pattern('--max-model-len'), text)
        if not anchor:
            raise ValueError('Не найдено место добавления флага; файл не изменён.')
        return text[:anchor.start()] + flag + ' ' + text[anchor.start():]
    return text


def configure(script, profile):
    if profile not in PROFILES:
        raise ValueError('Неизвестный профиль.')
    script = Path(script)
    if not script.is_absolute() or script.suffix != '.sh':
        raise ValueError('Нужен абсолютный путь к .sh-файлу.')
    metadata = files.script_metadata(script)
    source = script.read_bytes()
    if len(source) > 65536 or not source.startswith(b'#!'):
        raise ValueError('Нужен shell-скрипт с shebang размером до 64 КиБ.')
    text = source.decode('utf-8')
    if any(token.split('=', 1)[0] == '--kv-cache-memory-bytes' for token in tokens(text)):
        raise ValueError('Задан --kv-cache-memory-bytes: он переопределяет долю памяти. Файл не изменён.')
    for flag, value in PROFILES[profile].items():
        text = numeric(text, flag, value)
    text = boolean(text, '--enable-chunked-prefill', True)
    text = boolean(text, '--enforce-eager', profile == '128k')
    candidate = text.encode('utf-8')
    result = {'profile': profile, 'script': str(script), 'values': PROFILES[profile],
              'enforce_eager': profile == '128k', 'restart_performed': False}
    if candidate == source:
        files.run(['env', '-u', 'BASH_ENV', '-u', 'ENV', 'bash', '--noprofile', '--norc', '-n', str(script)])
        return result | {'state': 'already_configured'}
    folder = Path(tempfile.mkdtemp(prefix='qwen-profile-' + profile + '-', dir=BACKUPS))
    files.private_write(folder / 'original.sh', source)
    files.private_write(folder / 'metadata.json', json.dumps(metadata).encode())
    files.private_write(folder / 'candidate.sh', candidate)
    try:
        files.run(['env', '-u', 'BASH_ENV', '-u', 'ENV', 'bash', '--noprofile', '--norc', '-n', str(folder / 'candidate.sh')])
    except ValueError as error:
        raise ValueError('bash -n отклонил кандидат; исходный файл не изменён. Содержимое проверки скрыто.') from error
    files.replace_script(script, files.digest(source), candidate, metadata)
    result.update(state='updated', backup=str(folder / 'original.sh'))
    if profile == '128k':
        executable = shlex.quote(str(Path(__file__).resolve()))
        result['rollback_command'] = f'sudo python3 {executable} --profile 32k --script {shlex.quote(str(script))}'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=PROFILES, required=True)
    parser.add_argument('--script', type=Path, default=Path('/home/user/1Cat-vLLM/start_vllm38.sh'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.exit(1, 'Запустите через sudo.\n')
    os.umask(0o077)
    try:
        result = configure(args.script, args.profile)
    except (OSError, ValueError) as error:
        message = str(error) if type(error) is ValueError else 'Ошибка чтения или записи; содержимое файла и ключи скрыты.'
        parser.exit(1, message + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
