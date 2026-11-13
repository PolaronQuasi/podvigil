import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import * as prettier from 'prettier';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const diagrams = join(root, 'docs', 'diagrams');
const cli = join(root, 'node_modules', '@mermaid-js', 'mermaid-cli', 'src', 'cli.js');
if (!existsSync(cli)) throw new Error('Run npm ci before rendering diagrams.');
const sources = readdirSync(diagrams)
  .filter((name) => name.endsWith('.mmd'))
  .sort();
const png = process.argv.includes('--png');
const destination = png ? join(root, 'output', 'diagrams') : diagrams;
mkdirSync(destination, { recursive: true });

function decodeXmlText(text) {
  const entities = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" };
  return text.replace(/&(#x[0-9a-fA-F]+|#[0-9]+|amp|lt|gt|quot|apos);/g, (match, entity) => {
    if (entity.startsWith('#x')) return String.fromCodePoint(Number.parseInt(entity.slice(2), 16));
    if (entity.startsWith('#')) return String.fromCodePoint(Number.parseInt(entity.slice(1), 10));
    return entities[entity];
  });
}

async function formatSvg(path) {
  const options = await prettier.resolveConfig(path);
  let source = readFileSync(path, 'utf8');

  // Mermaid emits minified CSS and XML. Make both layers readable without
  // changing their contents.
  for (const match of source.matchAll(/<style([^>]*)>([\s\S]*?)<\/style>/g)) {
    const css = await prettier.format(decodeXmlText(match[2]), { ...options, parser: 'css' });
    const escaped = css.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    source = source.replace(match[0], `<style${match[1]}>\n${escaped}</style>`);
  }

  // Spaces between nested SVG tspans affect label positioning. Format their
  // attributes in strict mode, then protect that whitespace from later passes.
  for (const match of source.matchAll(/<text\b[^>]*>[\s\S]*?<\/text>/g)) {
    const label = await prettier.format(match[0], {
      ...options,
      parser: 'xml',
      xmlWhitespaceSensitivity: 'strict',
    });
    source = source.replace(
      match[0],
      `<!-- prettier-ignore-start -->\n${label}<!-- prettier-ignore-end -->`,
    );
  }

  const formatted = await prettier.format(source, { ...options, parser: 'xml' });
  writeFileSync(path, formatted);
}

for (const source of sources) {
  const output = join(destination, source.replace(/\.mmd$/, png ? '.png' : '.svg'));
  const result = spawnSync(
    process.execPath,
    [
      cli,
      '-i',
      join(diagrams, source),
      '-o',
      output,
      '-c',
      join(diagrams, 'mermaid.json'),
      '-b',
      'white',
    ],
    { cwd: root, env: process.env, stdio: 'inherit' },
  );
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
  if (!png) await formatSvg(output);
}
console.log(`Rendered ${sources.length} diagrams.`);
