import { readFileSync } from 'node:fs';
import { access } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [qmdDir, name, operation] = process.argv.slice(2);

function noteParts(body = '') {
  const heading = '\n## 확인한 사실\n';
  const start = body.indexOf(heading);
  if (start < 0) return { header: body, facts: '', offset: 0 };
  const offset = start + heading.length;
  return { header: body.slice(0, start), facts: body.slice(offset), offset };
}

async function recallNotes(store, query, remoteEmbedding, extractSnippet) {
  const maxReferences = 3;
  const excerptLimit = 1200;
  const options = { collection: name, limit: maxReferences * 3 };
  const ranked = new Map();
  function add(hit, weight) {
    if (hit.collectionName !== name || !hit.filepath.startsWith(`qmd://${name}/`)) return;
    const score = (ranked.get(hit.filepath)?.score ?? 0) + weight;
    ranked.set(hit.filepath, { hit, score });
  }

  // Full sentences are an AND query in QMD's keyword search. Search the terms
  // individually so exact identifiers still work when Ollama is offline.
  const terms = [...new Set(query.toLowerCase().match(/[\p{L}\p{N}_]{2,}/gu) ?? [])].slice(0, 12);
  for (const term of terms) {
    const sourceId = /^(?:t_[a-z0-9]+|[0-9a-f]{7,64})$/.test(term);
    for (const hit of await store.searchLex(term, options)) {
      const { header, facts } = noteParts(hit.body);
      // Match findings, or the pinned revisions of a provenance-only note.
      // Shared headers also contain boilerplate such as the project name.
      const text = sourceId ? header + facts : facts || (header.match(/^- (?:Git HEAD|원격 main) · .+$/gm) ?? []).join('\n');
      if (text.toLowerCase().includes(term)) add(hit, hit.score);
    }
  }
  if (remoteEmbedding) {
    try {
      const hits = await store.searchVector(query, { ...options, signal: AbortSignal.timeout(4500) });
      for (const hit of hits) {
        // A nearest neighbour is returned even for an unrelated question.
        if (hit.score >= 0.55) add(hit, hit.score);
      }
    } catch {
      // The lexical matches remain usable when the embedding endpoint is unavailable.
    }
  }

  const references = [];
  for (const { hit } of [...ranked.values()].sort((a, b) => b.score - a.score)) {
    const { header, facts, offset } = noteParts(hit.body);
    const source = header.split('\n')
      .filter(line => /^- (완료|보드|카드|Git HEAD|원격 main) · /.test(line))
      .slice(0, 10).map(line => line.slice(2, 200));
    if (!facts.trim() && !source.some(line => /^(Git HEAD|원격 main) · /.test(line))) continue;
    const excerpt = facts.length <= excerptLimit ? facts.trim()
      : extractSnippet(facts, query, excerptLimit, Math.max(0, (hit.chunkPos ?? 0) - offset)).snippet;
    references.push({ document: hit.filepath, source, excerpt });
    if (references.length === maxReferences) break;
  }
  return references;
}

async function run() {
  const input = JSON.parse(readFileSync(0, 'utf8'));
  const configPath = join(qmdDir, 'index.yml');
  const dbPath = join(qmdDir, 'index.sqlite');
  await access(configPath);
  if (operation === '--recall') await access(dbPath);
  const sdk = join(homedir(), '.local/share/hermes-qmd/lib/node_modules/@tobilu/qmd/dist/index.js');
  const { createStore, Maintenance, extractSnippet } = await import(pathToFileURL(sdk));
  const store = await createStore({ dbPath, configPath });
  try {
    const path = join(qmdDir, 'knowledge', name);
    const existing = (await store.listCollections()).find(collection => collection.name === name);
    if (existing && (resolve(existing.pwd) !== path || existing.glob_pattern !== '*.md')) {
      throw new Error(`Collection ${name} already points to a different path or file pattern`);
    }
    const { loadConfig, updateCollectionSettings } = await import(new URL('./collections.js', pathToFileURL(sdk)));
    if (operation === '--recall') {
      const remoteEmbedding = /^https?:\/\//.test(loadConfig().models?.embed ?? '');
      const references = existing?.active_count ? await recallNotes(store, input, remoteEmbedding, extractSnippet) : [];
      console.log(JSON.stringify(references));
      return;
    }
    if (operation === '--remove') {
      // Also finishes a previous removal interrupted between the YAML and SQLite writes.
      await store.removeCollection(name);
      new Maintenance(store.internal).cleanupOrphanedVectors();
      return;
    }
    if (!existing) await store.addCollection(name, { path, pattern: '*.md' });
    if (loadConfig().collections[name]?.includeByDefault !== false) {
      updateCollectionSettings(name, { includeByDefault: false });
    }
    const filenames = input;
    if (filenames.length === 0) return;
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
  await run();
} catch (error) {
  console.error(`QMD ${name} · ${error.message}`);
  process.exitCode = 1;
}
