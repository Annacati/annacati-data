// Minimal RFC-4180 CSV for GTFS .txt tables: a parser that handles quoted
// fields (commas, quotes, newlines inside quotes), a UTF-8 BOM, and CRLF/LF, and
// an emitter that quotes only when needed. Rows are string maps keyed by header.

export type Row = Record<string, string>;

export interface Table {
  header: string[];
  rows: Row[];
}

export function parseCsv(text: string): Table {
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1); // strip BOM
  const records: string[][] = [];
  let field = "";
  let record: string[] = [];
  let inQuotes = false;
  let i = 0;
  const n = text.length;
  while (i < n) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        inQuotes = false;
        i++;
        continue;
      }
      field += c;
      i++;
      continue;
    }
    if (c === '"') {
      inQuotes = true;
      i++;
      continue;
    }
    if (c === ",") {
      record.push(field);
      field = "";
      i++;
      continue;
    }
    if (c === "\r") {
      i++;
      continue;
    }
    if (c === "\n") {
      record.push(field);
      records.push(record);
      field = "";
      record = [];
      i++;
      continue;
    }
    field += c;
    i++;
  }
  // flush trailing field/record (file not ending in newline)
  if (field.length > 0 || record.length > 0) {
    record.push(field);
    records.push(record);
  }
  if (records.length === 0) return { header: [], rows: [] };
  const header = records[0];
  const rows: Row[] = [];
  for (let r = 1; r < records.length; r++) {
    const rec = records[r];
    if (rec.length === 1 && rec[0] === "") continue; // skip blank line
    const row: Row = {};
    for (let c = 0; c < header.length; c++) row[header[c]] = rec[c] ?? "";
    rows.push(row);
  }
  return { header, rows };
}

function needsQuote(s: string): boolean {
  return s.includes(",") || s.includes('"') || s.includes("\n") || s.includes("\r");
}

function quote(s: string): string {
  return needsQuote(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

// Emit a table. `header` is the canonical column order; any key present in rows
// but absent from it (a column a transform added) is appended so nothing drops.
export function emitCsv(rows: Row[], header: string[]): string {
  const cols = [...header];
  for (const row of rows) {
    for (const k of Object.keys(row)) {
      if (!cols.includes(k)) cols.push(k);
    }
  }
  const lines = [cols.map(quote).join(",")];
  for (const row of rows) {
    lines.push(cols.map((c) => quote(row[c] ?? "")).join(","));
  }
  return lines.join("\r\n") + "\r\n";
}
