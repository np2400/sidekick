import { Fragment, type ReactNode } from "react";

/**
 * A reply's text with the bits of Markdown models use: tables, bullet and numbered lists, **bold**, *italic*,
 * `code` and links. The text is untrusted (a model wrote it, maybe after reading an email or a web page), so it's
 * only ever turned into React elements, never HTML: anything else in it shows as the plain text it is. Links open
 * only for http(s) addresses, in a new tab, without telling the page where the visitor came from.
 */
export function RichText({ text }: { text: string }) {
  return <>{blocks(text.replace(/\r\n?/g, "\n").split("\n"))}</>;
}

const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
// A list item: its indent, its marker (a bullet or "1."), its text. More indented items nest under the one above.
const ITEM = /^(\s*)([*\-•]|\d{1,3}[.)])\s+(.*)$/;
type Item = { indent: number; numbered: boolean; text: string; children: Item[] };

function blocks(lines: string[]): ReactNode[] {
  const out: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (TABLE_ROW.test(line) && TABLE_RULE.test(lines[i + 1] ?? "")) {
      const rows = [line];
      for (i += 2; i < lines.length && TABLE_ROW.test(lines[i]); i++) rows.push(lines[i]);
      out.push(table(rows, out.length));
    } else if (ITEM.test(line)) {
      const items: Item[] = [];
      for (; i < lines.length && ITEM.test(lines[i]); i++) {
        const [, indent, marker, text] = lines[i].match(ITEM)!;
        const item = { indent: indent.length, numbered: /\d/.test(marker), text, children: [] };
        const parent = items[items.length - 1];
        if (parent && item.indent >= parent.indent + 2) parent.children.push(item);  // one level deep is enough
        else if (parent && item.numbered !== items[0].numbered) break;  // "1." after bullets: a new list
        else items.push(item);
      }
      out.push(list(items, out.length));
    } else if (!line.trim()) {
      i++;
    } else {
      const paragraph: string[] = [];
      for (; i < lines.length && lines[i].trim() && !startsBlock(lines, i); i++) paragraph.push(lines[i]);
      out.push(<p key={out.length} className="md-p">
        {paragraph.map((l, n) => <Fragment key={n}>{n > 0 && <br />}{inline(l)}</Fragment>)}
      </p>);
    }
  }
  return out;
}

function startsBlock(lines: string[], i: number): boolean {
  return ITEM.test(lines[i]) || (TABLE_ROW.test(lines[i]) && TABLE_RULE.test(lines[i + 1] ?? ""));
}

function list(items: Item[], key: number): ReactNode {
  const List = items[0].numbered ? "ol" : "ul";
  return (
    <List key={key} className="md-list">
      {items.map((item, n) => <li key={n}>{inline(item.text)}{item.children.length > 0 && list(item.children, 0)}</li>)}
    </List>
  );
}

function cells(row: string): string[] {
  return row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

/** Wide tables scroll sideways inside the bubble on a phone instead of squeezing every column. */
function table(rows: string[], key: number): ReactNode {
  const [head, ...body] = rows.map(cells);
  return (
    <div key={key} className="md-table-wrap">
      <table className="md-table">
        <thead><tr>{head.map((c, n) => <th key={n} scope="col">{inline(c)}</th>)}</tr></thead>
        <tbody>{body.map((r, n) => <tr key={n}>{head.map((_, m) => <td key={m}>{inline(r[m] ?? "")}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

// One pass over a line: `code`, [text](https://link), a bare https://link, **bold**, *italic*.
const INLINE = /`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>"')\]]+)|\*\*([^*]+)\*\*|\*([^*\s][^*]*[^*\s]|[^*\s])\*/g;

function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    const key = out.length;
    if (m[1] !== undefined) out.push(<code key={key}>{m[1]}</code>);
    else if (m[3] !== undefined) out.push(link(m[3], m[2], key));
    else if (m[4] !== undefined) {
      const url = m[4].replace(/[.,;:!?]+$/, "");  // "see https://x.example." -> the link without its full stop
      out.push(link(url, url, key), m[4].slice(url.length));
    }
    else if (m[5] !== undefined) out.push(<strong key={key}>{m[5]}</strong>);
    else out.push(<em key={key}>{m[6]}</em>);
    last = at + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function link(url: string, label: string, key: number): ReactNode {
  return <a key={key} className="msg-link" href={url} target="_blank" rel="noopener noreferrer nofollow">{label}</a>;
}
