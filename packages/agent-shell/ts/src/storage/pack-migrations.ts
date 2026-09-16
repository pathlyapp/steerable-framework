/**
 * 包迁移注册表 —— ScenarioPack 的存储槽位（0.3b）。
 *
 * `storage/index.ts` 的 `LocalStore` 单例在模块求值时即构造并跑 `migrate()`，
 * 所以包迁移必须在任何 `storage/index.js` import 之前注册。入口
 * （main.ts / server/index.ts）把 `packs/active.ts` 作为第一个 import
 * 来保证这个顺序。
 *
 * 本模块刻意不 import `storage/index.js`，避免反向依赖。
 */

import type { MigrationContribution } from '../scenario/pack.js';

interface RegisteredMigration {
  packId: string;
  migration: MigrationContribution;
}

const registered: RegisteredMigration[] = [];

/**
 * 注册一个包的迁移贡献。重复注册同一包会抛错（组装期笔误，fail fast）。
 */
export function registerPackMigrations(packId: string, migration: MigrationContribution): void {
  if (registered.some((r) => r.packId === packId)) {
    throw new Error(`[pack-migrations] duplicate registration for pack: ${packId}`);
  }
  registered.push({ packId, migration });
}

/** 已注册的全部迁移，按注册顺序返回。 */
export function getPackMigrations(): readonly RegisteredMigration[] {
  return registered;
}
