import { mkdtemp, copyFile, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';

const temp = await mkdtemp(join(tmpdir(), 'tracehelix-contract-'));
const files = ['../src/TraceHelix.Api/openapi/v1.json', 'src/api/generated.ts'];
try {
  await Promise.all(files.map((file, index) => copyFile(file, join(temp, String(index)))));
  const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
  const result = spawnSync(npm, ['run', 'generate:api'], { stdio: 'inherit' });
  if (result.status !== 0) process.exit(result.status ?? 1);
  for (let index = 0; index < files.length; index++) {
    const [before, after] = await Promise.all([readFile(join(temp, String(index))), readFile(files[index])]);
    if (!before.equals(after)) {
      console.error(`Generated artifact is stale: ${files[index]}`);
      process.exitCode = 1;
    }
  }
} finally {
  await rm(temp, { recursive: true, force: true });
}
