import { readFileSync } from 'node:fs';
import { access } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [qmdDir, name] = process.argv.slice(2);

async function indexNotes() {
  const filenames = JSON.parse(readFileSync(0, 'utf8'));
  const configPath = join(qmdDir, 'index.yml');
  await access(configPath);
  const sdk = join(homedir(), '.local/share/hermes-qmd/lib/node_modules/@tobilu/qmd/dist/index.js');
  const { createStore } = await import(pathToFileURL(sdk));
  const store = await createStore({ dbPath: join(qmdDir, 'index.sqlite'), configPath });
  try {
    const path = join(qmdDir, 'knowledge', name);
    const existing = (await store.listCollections()).find(collection => collection.name === name);
    if (!existing) {
      await store.addCollection(name, { path, pattern: '*.md' });
    } else if (resolve(existing.pwd) !== path || existing.glob_pattern !== '*.md') {
      throw new Error(`Collection ${name} already points to a different path or file pattern`);
    }
    const updated = await store.update({ collections: [name] });
    if (updated.collections !== 1 || updated.skipped) throw new Error('Could not index every note');
    for (const filename of filenames) {
      const document = await store.get(`qmd://${name}/${filename}`, { includeBody: false });
      if ('error' in document) throw new Error(`Could not index ${filename}`);
    }
    const signal = AbortSignal.timeout(300_000);
    const embedded = await store.embed({ collection: name, signal });
    signal.throwIfAborted();
    if (embedded.errors) throw new Error(`Failed to embed ${embedded.errors} chunks`);
  } finally {
    await store.close();
  }
}

try {
  await indexNotes();
} catch (error) {
  console.error(`QMD ${name} · ${error.message}`);
  process.exitCode = 1;
}
