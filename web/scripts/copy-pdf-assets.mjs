import { cpSync, existsSync, mkdirSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const source = join(root, 'node_modules', 'pdfjs-dist');
const destination = join(root, 'public', 'pdfjs');
for (const folder of ['cmaps', 'standard_fonts', 'iccs', 'wasm']) {
  const from = join(source, folder);
  if (!existsSync(from)) throw new Error(`PDF.js assets missing: ${from}`);
  mkdirSync(join(destination, folder), { recursive: true });
  for (const name of readdirSync(from)) {
    // The canvas-only viewer never executes PDF JavaScript. Do not ship the
    // optional QuickJS execution engine, and avoid unused WASM decoders.
    if (name.startsWith('quickjs') || name.endsWith('.wasm')) continue;
    cpSync(join(from, name), join(destination, folder, name));
  }
}
cpSync(join(source, 'LICENSE'), join(destination, 'LICENSE'));
