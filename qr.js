// QR Code Generator - Pure Vanilla JS (no dependencies)
// Byte mode, Error Correction Level L, Versions 1-10
// Safe for embedding in Python triple-quoted strings:
//   No backticks, no const/let, no problematic backslashes.
var generateQR, renderQR;
(function() {
  // --- Lookup Tables (EC Level L only) ---
  // [ecPerBlock, blocksG1, dcwG1, blocksG2, dcwG2]
  var EC = [null,
    [7,1,19,0,0],[10,1,34,0,0],[15,1,55,0,0],[20,1,80,0,0],[26,1,108,0,0],
    [18,2,68,0,0],[20,2,78,0,0],[24,2,97,0,0],[30,2,116,0,0],[18,2,68,2,69]];
  // Max byte-mode data capacity per version
  var CAP = [0, 17, 32, 53, 78, 106, 134, 154, 192, 230, 271];
  // Alignment pattern center coordinates per version
  var AL = [null,[],[6,18],[6,22],[6,26],[6,30],
    [6,34],[6,22,38],[6,24,42],[6,26,46],[6,28,50]];

  // --- GF(256) Arithmetic (primitive poly 0x11d) ---
  var GE = new Array(512), GL = new Array(256);
  (function() {
    var x = 1;
    for (var i = 0; i < 255; i++) {
      GE[i] = x; GL[x] = i;
      x <<= 1;
      if (x > 255) x ^= 0x11d;
    }
    for (var i = 255; i < 512; i++) GE[i] = GE[i - 255];
  })();
  function gm(a, b) { return a && b ? GE[GL[a] + GL[b]] : 0; }

  // --- Reed-Solomon Error Correction ---
  // Build generator polynomial of degree n, coefficients high-to-low
  // g(x) = prod_{i=0}^{n-1} (x + alpha^i)
  function rsg(n) {
    var p = [1], i, j, q;
    for (i = 0; i < n; i++) {
      q = new Array(p.length + 1);
      for (j = 0; j <= p.length; j++) q[j] = 0;
      for (j = 0; j < p.length; j++) {
        q[j] ^= p[j];                  // x * p contribution
        q[j + 1] ^= gm(p[j], GE[i]);  // alpha^i * p contribution
      }
      p = q;
    }
    return p;
  }
  // Compute EC codewords via polynomial long division
  function rse(data, ne) {
    var g = rsg(ne), r = [], i, j, c;
    for (i = 0; i < ne; i++) r[i] = 0;
    for (i = 0; i < data.length; i++) {
      c = data[i] ^ r[0];
      for (j = 0; j < ne - 1; j++) r[j] = r[j + 1];
      r[ne - 1] = 0;
      if (c) for (j = 0; j < ne; j++) r[j] ^= gm(g[j + 1], c);
    }
    return r;
  }

  // --- Byte-mode data encoding ---
  function encode(text, ver) {
    var ec = EC[ver], ndc = ec[1] * ec[2] + ec[3] * ec[4];
    var bits = [], i, j, b;
    function pb(v, n) { for (var k = n - 1; k >= 0; k--) bits.push((v >> k) & 1); }
    pb(4, 4);                                   // mode indicator: byte=0100
    pb(text.length, ver <= 9 ? 8 : 16);         // character count
    for (i = 0; i < text.length; i++) pb(text.charCodeAt(i), 8); // data
    pb(0, Math.min(4, ndc * 8 - bits.length));  // terminator
    while (bits.length % 8) bits.push(0);       // byte-align
    var db = [];
    for (i = 0; i < bits.length; i += 8) {
      b = 0; for (j = 0; j < 8; j++) b = (b << 1) | bits[i + j]; db.push(b);
    }
    var pi = 0;
    while (db.length < ndc) db.push(pi++ % 2 === 0 ? 236 : 17); // pad codewords
    return db;
  }

  // --- Interleave data and EC blocks ---
  function interleave(db, ver) {
    var ec = EC[ver], ne = ec[0], b1 = ec[1], d1 = ec[2], b2 = ec[3], d2 = ec[4];
    var blocks = [], ecb = [], off = 0, i, j, bl;
    for (i = 0; i < b1; i++) {
      bl = db.slice(off, off + d1); blocks.push(bl); ecb.push(rse(bl, ne)); off += d1;
    }
    for (i = 0; i < b2; i++) {
      bl = db.slice(off, off + d2); blocks.push(bl); ecb.push(rse(bl, ne)); off += d2;
    }
    var res = [], mx = Math.max(d1, d2 || 0);
    for (i = 0; i < mx; i++)
      for (j = 0; j < blocks.length; j++)
        if (i < blocks[j].length) res.push(blocks[j][i]);
    for (i = 0; i < ne; i++)
      for (j = 0; j < ecb.length; j++) res.push(ecb[j][i]);
    return res;
  }

  // --- Matrix helpers ---
  // m = module values (bool), r = reserved flags, sz = side length
  function mk(sz) {
    var m = [], r = [], i, j;
    for (i = 0; i < sz; i++) {
      m[i] = []; r[i] = [];
      for (j = 0; j < sz; j++) { m[i][j] = false; r[i][j] = false; }
    }
    return { m: m, r: r, sz: sz };
  }
  function cp(g) {
    var n = mk(g.sz), i, j;
    for (i = 0; i < g.sz; i++)
      for (j = 0; j < g.sz; j++) { n.m[i][j] = g.m[i][j]; n.r[i][j] = g.r[i][j]; }
    return n;
  }

  // --- Place function patterns ---
  // 7x7 finder pattern + 1-module white separator at (row,col)
  function placeFinder(g, row, col) {
    for (var dr = -1; dr <= 7; dr++)
      for (var dc = -1; dc <= 7; dc++) {
        var rr = row + dr, cc = col + dc;
        if (rr < 0 || rr >= g.sz || cc < 0 || cc >= g.sz) continue;
        g.m[rr][cc] = (dr >= 0 && dr <= 6 && dc >= 0 && dc <= 6) &&
          (dr === 0 || dr === 6 || dc === 0 || dc === 6 ||
           (dr >= 2 && dr <= 4 && dc >= 2 && dc <= 4));
        g.r[rr][cc] = true;
      }
  }
  // 5x5 alignment patterns (skip if overlapping finder)
  function placeAlign(g, ver) {
    var pos = AL[ver]; if (pos.length < 2) return;
    for (var i = 0; i < pos.length; i++)
      for (var j = 0; j < pos.length; j++) {
        if (g.r[pos[i]][pos[j]]) continue;
        for (var dr = -2; dr <= 2; dr++)
          for (var dc = -2; dc <= 2; dc++) {
            g.m[pos[i]+dr][pos[j]+dc] =
              Math.abs(dr) === 2 || Math.abs(dc) === 2 || (dr === 0 && dc === 0);
            g.r[pos[i]+dr][pos[j]+dc] = true;
          }
      }
  }
  // Timing patterns (row 6, col 6)
  function placeTiming(g) {
    for (var i = 8; i < g.sz - 8; i++) {
      if (!g.r[6][i]) { g.m[6][i] = i % 2 === 0; g.r[6][i] = true; }
      if (!g.r[i][6]) { g.m[i][6] = i % 2 === 0; g.r[i][6] = true; }
    }
  }
  // Reserve format-info areas and place the always-dark module
  function reserveFormat(g) {
    var i, s = g.sz;
    for (i = 0; i < 8; i++) { g.r[8][i] = true; g.r[i][8] = true; g.r[8][s-1-i] = true; }
    g.r[8][8] = true;
    for (i = 0; i < 7; i++) g.r[s-1-i][8] = true;
    g.m[s - 8][8] = true; g.r[s - 8][8] = true; // dark module
  }

  // --- Place data codewords in zigzag pattern ---
  function placeData(g, cw) {
    var bits = [], i, j;
    for (i = 0; i < cw.length; i++)
      for (j = 7; j >= 0; j--) bits.push((cw[i] >> j) & 1);
    var bi = 0, up = true;
    for (var col = g.sz - 1; col >= 1; col -= 2) {
      if (col === 6) col = 5; // skip timing column
      for (var ri = 0; ri < g.sz; ri++) {
        var row = up ? g.sz - 1 - ri : ri;
        for (var dc = 0; dc <= 1; dc++) {
          var c = col - dc;
          if (!g.r[row][c] && bi < bits.length) {
            g.m[row][c] = bits[bi] === 1; bi++;
          }
        }
      }
      up = !up;
    }
  }

  // --- Masking ---
  var MF = [
    function(r,c) { return (r+c) % 2 === 0; },
    function(r,c) { return r % 2 === 0; },
    function(r,c) { return c % 3 === 0; },
    function(r,c) { return (r+c) % 3 === 0; },
    function(r,c) { return (((r/2)|0) + ((c/3)|0)) % 2 === 0; },
    function(r,c) { return (r*c) % 2 + (r*c) % 3 === 0; },
    function(r,c) { return ((r*c) % 2 + (r*c) % 3) % 2 === 0; },
    function(r,c) { return ((r+c) % 2 + (r*c) % 3) % 2 === 0; }
  ];
  function applyMask(g, mi) {
    var fn = MF[mi];
    for (var r = 0; r < g.sz; r++)
      for (var c = 0; c < g.sz; c++)
        if (!g.r[r][c] && fn(r, c)) g.m[r][c] = !g.m[r][c];
  }

  // --- Format information (BCH 15,5 encoding, EC Level L = 01) ---
  function writeFormat(g, mi) {
    var d = (1 << 3) | mi; // EC L (01) + mask pattern
    var tmp = d << 10;
    for (var i = 4; i >= 0; i--) if (tmp & (1 << (i + 10))) tmp ^= 0x537 << i;
    var fmt = ((d << 10) | tmp) ^ 0x5412;
    var s = g.sz;
    for (var i = 0; i < 15; i++) {
      var bit = ((fmt >> i) & 1) === 1;
      // Copy 1: around top-left finder
      if (i < 6)       g.m[8][i] = bit;
      else if (i === 6) g.m[8][7] = bit;
      else if (i === 7) g.m[8][8] = bit;
      else if (i === 8) g.m[7][8] = bit;
      else              g.m[14 - i][8] = bit; // i=9->(5,8) .. i=14->(0,8)
      // Copy 2: bottom-left vertical + top-right horizontal
      if (i < 7) g.m[s - 1 - i][8] = bit;
      else       g.m[8][s - 15 + i] = bit;   // i=7->(8,s-8) .. i=14->(8,s-1)
    }
  }

  // --- Penalty score (all 4 rules) ---
  function penalty(g) {
    var p = 0, s = g.sz, r, c, run;
    // Rule 1: consecutive same-color runs >= 5
    for (r = 0; r < s; r++) {
      run = 1;
      for (c = 1; c < s; c++) {
        if (g.m[r][c] === g.m[r][c-1]) run++;
        else { if (run >= 5) p += run - 2; run = 1; }
      }
      if (run >= 5) p += run - 2;
    }
    for (c = 0; c < s; c++) {
      run = 1;
      for (r = 1; r < s; r++) {
        if (g.m[r][c] === g.m[r-1][c]) run++;
        else { if (run >= 5) p += run - 2; run = 1; }
      }
      if (run >= 5) p += run - 2;
    }
    // Rule 2: 2x2 same-color blocks
    for (r = 0; r < s-1; r++)
      for (c = 0; c < s-1; c++) {
        var v = g.m[r][c];
        if (v === g.m[r][c+1] && v === g.m[r+1][c] && v === g.m[r+1][c+1]) p += 3;
      }
    // Rule 3: finder-like pattern 1011101-0000 or 0000-1011101
    var m = g.m;
    for (r = 0; r < s; r++)
      for (c = 0; c <= s - 11; c++) {
        if ( m[r][c] && !m[r][c+1] &&  m[r][c+2] &&  m[r][c+3] &&  m[r][c+4] &&
            !m[r][c+5] && m[r][c+6] && !m[r][c+7] && !m[r][c+8] && !m[r][c+9] && !m[r][c+10]) p += 40;
        if (!m[r][c] && !m[r][c+1] && !m[r][c+2] && !m[r][c+3] &&  m[r][c+4] &&
            !m[r][c+5] && m[r][c+6] &&  m[r][c+7] &&  m[r][c+8] && !m[r][c+9] &&  m[r][c+10]) p += 40;
      }
    for (c = 0; c < s; c++)
      for (r = 0; r <= s - 11; r++) {
        if ( m[r][c] && !m[r+1][c] &&  m[r+2][c] &&  m[r+3][c] &&  m[r+4][c] &&
            !m[r+5][c] && m[r+6][c] && !m[r+7][c] && !m[r+8][c] && !m[r+9][c] && !m[r+10][c]) p += 40;
        if (!m[r][c] && !m[r+1][c] && !m[r+2][c] && !m[r+3][c] &&  m[r+4][c] &&
            !m[r+5][c] && m[r+6][c] &&  m[r+7][c] &&  m[r+8][c] && !m[r+9][c] &&  m[r+10][c]) p += 40;
      }
    // Rule 4: dark module proportion deviation from 50%
    var dk = 0;
    for (r = 0; r < s; r++) for (c = 0; c < s; c++) if (g.m[r][c]) dk++;
    var pct = dk * 100 / (s * s);
    var p5 = Math.floor(pct / 5) * 5;
    p += Math.min(Math.abs(p5 - 50) / 5, Math.abs(p5 + 5 - 50) / 5) * 10;
    return p;
  }

  // --- Public API ---
  generateQR = function(text) {
    // Auto-select smallest version that fits the data
    var ver = 0;
    for (var v = 1; v <= 10; v++) if (text.length <= CAP[v]) { ver = v; break; }
    if (!ver) throw new Error("Text too long (max 271 bytes for versions 1-10, EC Level L)");
    var sz = (ver - 1) * 4 + 21;
    var cw = interleave(encode(text, ver), ver);
    // Build base matrix with all function patterns + data
    var base = mk(sz);
    placeFinder(base, 0, 0);
    placeFinder(base, 0, sz - 7);
    placeFinder(base, sz - 7, 0);
    placeAlign(base, ver);
    placeTiming(base);
    reserveFormat(base);
    placeData(base, cw);
    // Try all 8 mask patterns, pick the one with lowest penalty
    var bestP = Infinity, bestG = null;
    for (var mi = 0; mi < 8; mi++) {
      var g = cp(base);
      applyMask(g, mi);
      writeFormat(g, mi);
      var score = penalty(g);
      if (score < bestP) { bestP = score; bestG = g; }
    }
    return bestG.m;
  };

  renderQR = function(canvas, matrix, scale) {
    var sz = matrix.length, s = scale || 4, q = 4; // q = quiet zone modules
    var total = (sz + q * 2) * s;
    canvas.width = total; canvas.height = total;
    var ctx = canvas.getContext("2d");
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, total, total);
    ctx.fillStyle = "#000000";
    for (var r = 0; r < sz; r++)
      for (var c = 0; c < sz; c++)
        if (matrix[r][c]) ctx.fillRect((c + q) * s, (r + q) * s, s, s);
  };
})();
