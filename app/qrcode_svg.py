"""Générateur de QR code ISO/IEC 18004 (versions 1 à 6, mode octet, correction M) en Python pur.

Aucune dépendance externe : sert à afficher le QR code de couplage Telegram directement en SVG local.
"""
from __future__ import annotations

# (version, taille, nb_blocs, octets_données_par_bloc, octets_ec_par_bloc, coordonnées_alignement)
_VERSIONS_M = (
    (1, 21, 1, 16, 10, ()),
    (2, 25, 1, 28, 16, (6, 18)),
    (3, 29, 1, 44, 26, (6, 22)),
    (4, 33, 2, 32, 18, (6, 26)),
    (5, 37, 2, 43, 24, (6, 30)),
    (6, 41, 4, 27, 16, (6, 34)),
)
_EC_LEVEL_M = 0b00   # ISO/IEC 18004 Table 25 : L=01, M=00, Q=11, H=10


def _gf_mul(a: int, b: int) -> int:
    res = 0
    while b:
        if b & 1:
            res ^= a
        a <<= 1
        if a & 0x100:
            a ^= 0x11D
        b >>= 1
    return res


def _rs_generator(degree: int) -> list[int]:
    poly = [1]
    root = 1
    for _ in range(degree):
        nxt = [0] * (len(poly) + 1)
        for i, c in enumerate(poly):
            nxt[i] ^= c
            nxt[i + 1] ^= _gf_mul(c, root)
        poly = nxt
        root = _gf_mul(root, 2)
    return poly


def _rs_remainder(data: list[int], ec_len: int) -> list[int]:
    gen = _rs_generator(ec_len)
    rem = [0] * ec_len
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        if factor:
            for i in range(ec_len):
                rem[i] ^= _gf_mul(gen[i + 1], factor)
    return rem


def _format_bits(ec_bits: int, mask: int) -> int:
    data = ((ec_bits & 0b11) << 3) | (mask & 0b111)
    rem = data << 10
    for shift in range(4, -1, -1):
        if rem & (1 << (shift + 10)):
            rem ^= 0x537 << shift
    return ((data << 10) | rem) ^ 0x5412


def _mask_fn(mask: int, r: int, c: int) -> bool:
    if mask == 0:
        return (r + c) % 2 == 0
    if mask == 1:
        return r % 2 == 0
    if mask == 2:
        return c % 3 == 0
    if mask == 3:
        return (r + c) % 3 == 0
    if mask == 4:
        return ((r // 2) + (c // 3)) % 2 == 0
    if mask == 5:
        return ((r * c) % 2) + ((r * c) % 3) == 0
    if mask == 6:
        return (((r * c) % 2) + ((r * c) % 3)) % 2 == 0
    return (((r + c) % 2) + ((r * c) % 3)) % 2 == 0


def _penalty(grid: list[list[int]]) -> int:
    size = len(grid)
    score = 0
    # Règle 1 : suites horizontales et verticales >= 5
    for line in list(grid) + [[grid[r][c] for r in range(size)] for c in range(size)]:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    # Règle 2 : blocs 2x2 uniformes
    for r in range(size - 1):
        for c in range(size - 1):
            v = grid[r][c]
            if v == grid[r][c + 1] == grid[r + 1][c] == grid[r + 1][c + 1]:
                score += 3
    # Règle 3 : motifs proches du finder (1011101 + 4 blancs)
    pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    for line in list(grid) + [[grid[r][c] for r in range(size)] for c in range(size)]:
        for i in range(size - 10):
            seg = line[i:i + 11]
            if seg == pat1 or seg == pat2:
                score += 40
    # Règle 4 : équilibre noir/blanc
    dark = sum(sum(row) for row in grid)
    percent = (dark * 100) // (size * size)
    score += (abs(percent - 50) // 5) * 10
    return score


def _place_format(grid: list[list[int]], mask: int) -> None:
    size = len(grid)
    bits = _format_bits(_EC_LEVEL_M, mask)
    bit = lambda idx: (bits >> idx) & 1  # noqa: E731
    # Copie 1 (autour du finder haut-gauche)
    for c in range(6):
        grid[8][c] = bit(14 - c)
    grid[8][7] = bit(8)
    grid[8][8] = bit(7)
    grid[7][8] = bit(6)
    for i in range(6):
        grid[5 - i][8] = bit(5 - i)
    # Copie 2 (bas-gauche + haut-droite)
    for i in range(7):
        grid[size - 1 - i][8] = bit(14 - i)
    grid[size - 8][8] = 1
    for i in range(8):
        grid[8][size - 8 + i] = bit(7 - i)


def encode_matrix(text: str) -> list[list[int]]:
    """Encode ``text`` (UTF-8, jusqu'à 106 octets) en matrice QR carrée de 0/1."""
    raw = text.encode("utf-8")
    spec = next((v for v in _VERSIONS_M if len(raw) <= v[2] * v[3] - 2), None)
    if spec is None:
        raise ValueError("Texte trop long pour le QR code (106 octets max)")
    _, size, blocks, data_per_block, ec_len, align = spec
    total_data = blocks * data_per_block

    # Flux de bits : mode octet (0100), longueur (8 bits), données, terminateur, remplissage 0xEC/0x11
    bits: list[int] = []

    def push(val: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            bits.append((val >> shift) & 1)

    push(0b0100, 4)
    push(len(raw), 8)
    for b in raw:
        push(b, 8)
    push(0, min(4, total_data * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    codewords = [sum(bits[i + b] << (7 - b) for b in range(8)) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    idx = 0
    while len(codewords) < total_data:
        codewords.append(pad[idx % 2])
        idx += 1

    # Découpage en blocs, calcul Reed-Solomon et entrelacement
    data_blocks = [codewords[i * data_per_block:(i + 1) * data_per_block] for i in range(blocks)]
    ec_blocks = [_rs_remainder(blk, ec_len) for blk in data_blocks]
    stream: list[int] = []
    for i in range(data_per_block):
        for blk in data_blocks:
            stream.append(blk[i])
    for i in range(ec_len):
        for blk in ec_blocks:
            stream.append(blk[i])
    stream_bits = [(byte >> shift) & 1 for byte in stream for shift in range(7, -1, -1)]

    # Construction du gabarit fonctionnel (finders, séparateurs, timing, alignement, zones de format)
    base = [[0] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def put_func(r: int, c: int, val: int) -> None:
        if 0 <= r < size and 0 <= c < size:
            base[r][c] = val
            reserved[r][c] = True

    for fr, fc in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                val = 1 if (0 <= dr <= 6 and 0 <= dc <= 6 and (
                    dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)
                )) else 0
                put_func(fr + dr, fc + dc, val)

    for i in range(8, size - 8):
        put_func(6, i, (i + 1) % 2)
        put_func(i, 6, (i + 1) % 2)

    for ar in align:
        for ac in align:
            if reserved[ar][ac]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    val = 1 if (max(abs(dr), abs(dc)) in (0, 2)) else 0
                    put_func(ar + dr, ac + dc, val)

    # Réserver les positions des bits de format (+ module sombre)
    for i in range(9):
        reserved[8][i] = True
        reserved[i][8] = True
    for i in range(8):
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True
    base[size - 8][8] = 1

    # Placement des bits en zigzag de droite à gauche (colonne 6 sautée)
    bit_idx = 0
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for dc in (0, -1):
                c = col + dc
                if not reserved[r][c]:
                    base[r][c] = stream_bits[bit_idx] if bit_idx < len(stream_bits) else 0
                    bit_idx += 1
        upward = not upward
        col -= 2

    # Choix du meilleur masque (0 à 7) selon le score de pénalité ISO
    best_grid: list[list[int]] | None = None
    best_score = 10**9
    for mask in range(8):
        cand = [[base[r][c] ^ (1 if (not reserved[r][c] and _mask_fn(mask, r, c)) else 0)
                 for c in range(size)] for r in range(size)]
        _place_format(cand, mask)
        sc = _penalty(cand)
        if sc < best_score:
            best_score = sc
            best_grid = cand
    return best_grid  # type: ignore[return-value]


def qr_svg(text: str, *, border: int = 4) -> str:
    """Retourne un SVG autonome (fond blanc, modules noirs) prêt à être affiché ou scanné."""
    matrix = encode_matrix(text)
    size = len(matrix)
    total = size + 2 * border
    parts = [f"M{c + border} {r + border}h1v1h-1z"
             for r, row in enumerate(matrix) for c, val in enumerate(row) if val]
    path = "".join(parts)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" '
            f'shape-rendering="crispEdges" role="img" aria-label="QR code Telegram">'
            f'<rect width="{total}" height="{total}" fill="#ffffff"/>'
            f'<path d="{path}" fill="#0f172a"/></svg>')
