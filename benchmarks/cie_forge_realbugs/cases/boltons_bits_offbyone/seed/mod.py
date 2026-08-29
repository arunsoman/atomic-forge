"""Pre-fix (buggy) Bits — boltons mathutils.py @ c1c25da^.

Off-by-one in the length guard: the largest value representable in `len_`
bits is `2 ** len_ - 1`, but the guard used `val > 2 ** len_`, so `Bits(4, 2)`
(the value 4, which needs 3 bits) was silently accepted and produced an
over-long Bits. The fix changes the guard to `val >= 2 ** len_`.
"""


class Bits:
    """An immutable bit-string / bit-array (minimal standalone extract)."""

    __slots__ = ('val', 'len')

    def __init__(self, val=0, len_=None):
        if type(val) is not int:
            if type(val) is list:
                val = ''.join(['1' if e else '0' for e in val])
            if type(val) is bytes:
                val = val.decode('ascii')
            if type(val) is str:
                if len_ is None:
                    len_ = len(val)
                    if val.startswith('0x'):
                        len_ = (len_ - 2) * 4
                if val.startswith('0x'):
                    val = int(val, 16)
                else:
                    if val:
                        val = int(val, 2)
                    else:
                        val = 0
            if type(val) is not int:
                raise TypeError(f'initialized with bad type: {type(val).__name__}')
        if val < 0:
            raise ValueError('Bits cannot represent negative values')
        if len_ is None:
            len_ = len(f'{val:b}')
        if val > 2 ** len_:  # BUG: should be val >= 2 ** len_
            raise ValueError(f'value {val} cannot be represented with {len_} bits')
        self.val = val  # data is stored internally as integer
        self.len = len_

    def __len__(self):
        return self.len

    def as_bin(self):
        return f'{{0:0{self.len}b}}'.format(self.val)

    def as_int(self):
        return self.val