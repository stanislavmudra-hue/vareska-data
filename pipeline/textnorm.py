"""Python mirror of the app's ``lib/logic/text_normalizer.dart`` (SPEC 7.2).

``fold`` -> ``tokens`` -> ``stem`` -> ``token_matches`` must behave exactly like
the Dart version so that the pipeline and the app agree on what an ingredient
name looks like. Keep the tables in sync with the Dart file.
"""
from __future__ import annotations

# Explicit diacritics map (same table as the Dart file).
_FOLD_MAP: dict[str, str] = {
    # a
    'á': 'a', 'ä': 'a', 'â': 'a', 'à': 'a', 'ã': 'a', 'å': 'a', 'ā': 'a',
    'ă': 'a', 'ą': 'a', 'ǎ': 'a', 'ả': 'a', 'ạ': 'a',
    'ắ': 'a', 'ằ': 'a', 'ẳ': 'a', 'ẵ': 'a', 'ặ': 'a',
    'ấ': 'a', 'ầ': 'a', 'ẩ': 'a', 'ẫ': 'a', 'ậ': 'a',
    'æ': 'a',
    # c
    'č': 'c', 'ç': 'c', 'ć': 'c', 'ĉ': 'c', 'ċ': 'c',
    # d
    'ď': 'd', 'đ': 'd', 'ð': 'd',
    # e
    'é': 'e', 'ě': 'e', 'ë': 'e', 'è': 'e', 'ê': 'e', 'ē': 'e', 'ę': 'e',
    'ė': 'e', 'ĕ': 'e', 'ẻ': 'e', 'ẽ': 'e', 'ẹ': 'e',
    'ế': 'e', 'ề': 'e', 'ể': 'e', 'ễ': 'e', 'ệ': 'e',
    # g
    'ğ': 'g', 'ģ': 'g', 'ġ': 'g',
    # h
    'ħ': 'h',
    # i
    'í': 'i', 'ï': 'i', 'ì': 'i', 'î': 'i', 'ī': 'i', 'ı': 'i', 'į': 'i',
    'ǐ': 'i', 'ỉ': 'i', 'ĩ': 'i', 'ị': 'i',
    # k, l
    'ķ': 'k',
    'ľ': 'l', 'ĺ': 'l', 'ł': 'l', 'ļ': 'l',
    # n
    'ň': 'n', 'ñ': 'n', 'ń': 'n', 'ņ': 'n',
    # o
    'ó': 'o', 'ö': 'o', 'ò': 'o', 'ô': 'o', 'õ': 'o', 'ø': 'o', 'ō': 'o',
    'ő': 'o', 'ơ': 'o', 'ǒ': 'o', 'ỏ': 'o', 'ọ': 'o',
    'ố': 'o', 'ồ': 'o', 'ổ': 'o', 'ỗ': 'o', 'ộ': 'o',
    'ớ': 'o', 'ờ': 'o', 'ở': 'o', 'ỡ': 'o', 'ợ': 'o',
    'œ': 'oe',
    # r
    'ř': 'r', 'ŕ': 'r',
    # s
    'š': 's', 'ś': 's', 'ş': 's', 'ș': 's', 'ŝ': 's',
    # t
    'ť': 't', 'þ': 't', 'ţ': 't', 'ț': 't',
    # u
    'ú': 'u', 'ů': 'u', 'ü': 'u', 'ù': 'u', 'û': 'u', 'ū': 'u', 'ű': 'u',
    'ư': 'u', 'ų': 'u', 'ǔ': 'u', 'ǚ': 'u', 'ủ': 'u', 'ũ': 'u', 'ụ': 'u',
    'ứ': 'u', 'ừ': 'u', 'ử': 'u', 'ữ': 'u', 'ự': 'u',
    # y
    'ý': 'y', 'ÿ': 'y', 'ỳ': 'y', 'ỷ': 'y', 'ỹ': 'y', 'ỵ': 'y',
    # z
    'ž': 'z', 'ż': 'z', 'ź': 'z',
    'ß': 'ss',
}

# Ordered suffix list, longest first (SPEC 7.2).
_SUFFIXES: tuple[str, ...] = (
    'ami', 'emi', 'ich', 'ech', 'ach', 'ata', 'ete',
    'um', 'am', 'ou', 'em', 'im', 'ym',
    'e', 'i', 'u', 'y', 'a', 'o',
)


def fold(s: str) -> str:
    """Lowercase, strip diacritics via the explicit map and turn every other
    non-``[a-z0-9]`` character into a space; runs collapsed, result trimmed."""
    out: list[str] = []
    last_space = True
    for ch in s.lower():
        if ('a' <= ch <= 'z') or ('0' <= ch <= '9'):
            out.append(ch)
            last_space = False
            continue
        mapped = _FOLD_MAP.get(ch) if ord(ch) >= 0x80 else None
        if mapped is not None:
            out.append(mapped)
            last_space = False
        else:
            if not last_space:
                out.append(' ')
            last_space = True
    r = ''.join(out)
    return r[:-1] if r.endswith(' ') else r


def tokens(s: str) -> list[str]:
    """Split a folded string on spaces, dropping tokens shorter than 2."""
    return [t for t in s.split(' ') if len(t) >= 2]


def stem(t: str) -> str:
    """Czech suffix stemmer for folded ASCII tokens; never cuts below 4 chars."""
    if len(t) < 5:
        return t
    for suf in _SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[:-len(suf)]
    return t


def normalize_tokens(s: str) -> list[str]:
    """fold -> tokens -> stem."""
    return [stem(t) for t in tokens(fold(s))]


def token_matches(query: str, indexed: str) -> bool:
    """A query token matches an index token when either is a prefix of the
    other and the shorter one has at least 3 characters."""
    if not query or not indexed:
        return False
    shorter = query if len(query) <= len(indexed) else indexed
    if len(shorter) < 3:
        return query == indexed
    return indexed.startswith(query) or query.startswith(indexed)


def all_tokens_match(query_tokens: list[str], title_tokens: list[str]) -> bool:
    """Every query token has a matching title token."""
    return all(any(token_matches(q, t) for t in title_tokens) for q in query_tokens)
