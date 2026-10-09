"""Format-only normalization; invalid nonblank lines remain editable."""
from dataclasses import dataclass, asdict
import re

MAX_CHARS = 100000
MAX_LINES = 500
MAX_PHRASE = 1000
SPACE = re.compile(r'[^\S\n]+')
SEPARATOR = re.compile(r'(?<!\S)[–—-](?!\S)')
LIST = re.compile(r'^(?:[•*] |\d+[.)] )')

@dataclass
class NormalizedInput:
    normalized_text: str
    pairs: list
    errors: list

    def response(self):
        return dict(normalized_text=self.normalized_text, valid_count=len(self.pairs), invalid_count=len(self.errors), errors=self.errors)


def phrase_error(pl, ru):
    if not pl:
        return 'Отсутствует польская фраза'
    if not ru:
        return 'Отсутствует перевод'
    if max(len(pl),len(ru)) > MAX_PHRASE:
        return 'Максимум 1000 символов на фразу'
    if any(ord(c) < 32 or ord(c) == 127 for c in pl + ru):
        return 'Недопустимый управляющий символ'
    return None


def normalize_input(text: str) -> NormalizedInput:
    if len(text) > MAX_CHARS:
        return NormalizedInput(text, [], [dict(line=0,text='',message='Максимум 100 000 символов')])
    text = text.replace('\r\n','\n').replace('\r','\n').replace('\u2028','\n').replace('\u2029','\n')
    lines = [SPACE.sub(' ',line).strip() for line in text.split('\n')]
    lines = [line for line in lines if line]
    if len(lines) > MAX_LINES:
        return NormalizedInput('\n'.join(lines), [], [dict(line=0,text='',message='Максимум 500 непустых строк')])
    pairs, errors, cleaned = [], [], []
    for number,line in enumerate(lines,1):
        # Strip explicit list prefixes. A lone '- перевод' remains a missing-PL error.
        line = LIST.sub('',line,count=1)
        if line.startswith('- ') and SEPARATOR.search(line[2:]):
            line = line[2:]
        match = SEPARATOR.search(line)
        if match:
            pl,ru = line[:match.start()].strip(),line[match.end():].strip()
            canonical = f'{pl} - {ru}'.strip()
            message = phrase_error(pl,ru)
        else:
            pl,ru = line,''
            canonical = line
            message = 'Отсутствует перевод'
        cleaned.append(canonical)
        if message:
            errors.append(dict(line=number,text=canonical,message=message))
        else:
            pairs.append(dict(pl=pl,ru=ru))
    if not cleaned:
        errors.append(dict(line=0,text='',message='Вставьте хотя бы одну пару слов'))
    return NormalizedInput('\n'.join(cleaned),pairs,errors)


def parse_text(text):
    result = normalize_input(text)
    return result.pairs, [f"Строка {e['line']}: {e['message']}\n{e['text']}" for e in result.errors]


def validate_pairs(pairs):
    if not isinstance(pairs,list) or not 1 <= len(pairs) <= MAX_LINES:
        raise ValueError('Ожидается от 1 до 500 пар')
    total = 0
    for pair in pairs:
        if not isinstance(pair,dict) or any(not isinstance(pair.get(k),str) for k in ('pl','ru')):
            raise ValueError('Каждая пара должна содержать строки pl и ru')
        error = phrase_error(pair['pl'].strip(),pair['ru'].strip())
        if error:
            raise ValueError(error)
        total += len(pair['pl']) + len(pair['ru'])
    if total > MAX_CHARS:
        raise ValueError('Максимум 100 000 символов')
