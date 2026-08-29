"""Pre-fix (buggy) singularize() — boltons strutils.py @ 1e61524^.

Words ending in a double 's' (glass, boss, kiss) are already singular;
their plurals end in 'sses' and are handled by the earlier '-ses' branch.
But the final `else: singular = word[:-1]` blindly strips the trailing 's',
turning 'glass' -> 'glas', 'boss' -> 'bos', breaking idempotency
(singularize('Glasses') == 'Glass', but 'Glass' must stay 'Glass'). The fix
adds an `elif word.endswith('ss'): return orig_word` branch before the
final else.
"""
_IRR_S2P = {'addendum': 'addenda', 'alga': 'algae', 'alumnus': 'alumni',
            'analysis': 'analyses', 'axis': 'axes', 'basis': 'bases',
            'cactus': 'cacti', 'child': 'children', 'corpus': 'corpora',
            'crisis': 'crises', 'criterion': 'criteria', 'curriculum': 'curricula',
            'datum': 'data', 'diagnosis': 'diagnoses', 'die': 'dice',
            'elf': 'elves', 'emphasis': 'emphases', 'erratum': 'errata',
            'foot': 'feet', 'goose': 'geese', 'half': 'halves', 'knife': 'knives',
            'leaf': 'leaves', 'life': 'lives', 'loaf': 'loaves', 'louse': 'lice',
            'man': 'men', 'matrix': 'matrices', 'medium': 'media', 'mouse': 'mice',
            'neurosis': 'neuroses', 'nucleus': 'nuclei', 'oasis': 'oases',
            'octopus': 'octopi', 'ox': 'oxen', 'parenthesis': 'parentheses',
            'phenomenon': 'phenomena', 'potato': 'potatoes', 'radius': 'radii',
            'self': 'selves', 'series': 'series', 'sheep': 'sheep',
            'shelf': 'shelves', 'species': 'species', 'stimulus': 'stimuli',
            'stratum': 'strata', 'syllabus': 'syllabi', 'thesis': 'theses',
            'thief': 'thieves', 'tomato': 'tomatoes', 'tooth': 'teeth',
            'veto': 'vetoes', 'wife': 'wives', 'wolf': 'wolves', 'woman': 'women'}

_IRR_P2S = {v: k for k, v in _IRR_S2P.items()}


def _match_case(master, disciple):
    if not master.strip():
        return disciple
    if master.lower() == master:
        return disciple.lower()
    elif master.upper() == master:
        return disciple.upper()
    elif master.title() == master:
        return disciple.title()
    return disciple


def singularize(word):
    """Semi-intelligently converts an English plural *word* to its
    singular form, preserving case pattern."""
    orig_word, word = word, word.strip().lower()
    if not word or word in _IRR_S2P:
        return orig_word

    irr_singular = _IRR_P2S.get(word)
    if irr_singular:
        singular = irr_singular
    elif not word.endswith('s'):
        return orig_word
    elif len(word) == 2:
        singular = word[:-1]  # or just return word?
    elif word.endswith('ies') and word[-4:-3] not in 'aeiou':
        singular = word[:-3] + 'y'
    elif word.endswith('es') and word[-3] == 's':
        singular = word[:-2]
    elif word.endswith('ss'):
        # Words ending in a double 's' are already singular.
        return orig_word
    else:
        singular = word[:-1]
    return _match_case(orig_word, singular)