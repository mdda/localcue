// Maps between a position in the script and a vertical pixel offset within a
// text element, so desktop and phone can show the same text on their reading
// lines despite wrapping the lines differently. Shared by desktop and mobile.
//
// pos (float, 0..text length): the integer part is a character index; the
//   fractional part interpolates down through that character's visual line,
//   so moving pos (or y) smoothly gives smooth motion across line breaks.
// y: pixels from the element's top border edge to the vertical centre of a line.
//
// `el` must contain a single text node (or nothing). Uses Range rects, which
// work even on a visibility:hidden element — hence the desktop's mirror div.
function createTextMapper(el, lineHeightFn) {
  const range = document.createRange();
  let originTop = 0;

  function len() {
    const t = el.firstChild;
    return t ? t.nodeValue.length : 0;
  }

  // Centre y of the visual line containing character i.
  function cy(i) {
    const t = el.firstChild;
    range.setStart(t, i);
    range.setEnd(t, i + 1);
    const r = range.getBoundingClientRect();
    return r.top + r.height / 2 - originTop;
  }

  // Smallest j in [lo, hi] for which the (monotonic false→true) pred holds; hi+1 if none.
  function search(lo, hi, pred) {
    while (lo <= hi) {
      const m = (lo + hi) >> 1;
      if (pred(m)) hi = m - 1; else lo = m + 1;
    }
    return lo;
  }

  // The visual line containing character i: chars [start, end), its centre y,
  // and the centre y of the following line.
  function line(i, n) {
    const y = cy(i);
    const start = search(0, i, (j) => cy(j) > y - 1);
    const end   = search(i + 1, n - 1, (j) => cy(j) > y + 1);  // n if i is on the last line
    const yNext = end < n ? cy(end) : y + lineHeightFn();
    return { start, end, y, yNext };
  }

  function begin() {
    originTop = el.getBoundingClientRect().top;
    return len();
  }

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  return {
    length: len,

    posToY(pos) {
      const n = begin();
      if (n === 0) return lineHeightFn() / 2;
      pos = clamp(pos, 0, n);
      const L = line(Math.min(Math.floor(pos), n - 1), n);
      return L.y + (pos - L.start) / (L.end - L.start) * (L.yNext - L.y);
    },

    yToPos(y) {
      const n = begin();
      if (n === 0 || y <= cy(0)) return 0;
      const i = search(0, n - 1, (j) => cy(j) > y) - 1;  // last char whose line centre is at/above y
      const L = line(i, n);
      const f = clamp((y - L.y) / (L.yNext - L.y), 0, 1);
      return L.start + f * (L.end - L.start);
    },

    // First character of the visual line containing character i.
    lineStart(i) {
      const n = begin();
      if (n === 0) return 0;
      return line(clamp(i, 0, n - 1), n).start;
    },
  };
}

// Reading line sits one third of the way down the screen on both devices.
const READING_LINE = 1 / 3;
