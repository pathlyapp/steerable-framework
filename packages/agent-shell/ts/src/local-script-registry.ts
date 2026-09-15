import Store from 'electron-store';
import { randomUUID } from 'crypto';

export interface LocalScript {
  id: string;
  name: string;
  description: string;
  command: string;
  cwd?: string;
  timeout?: number;
  tags?: string[];
  createdAt: string;
  updatedAt: string;
}

interface LocalScriptStore {
  localScripts: LocalScript[];
}

export interface CreateLocalScriptInput {
  name: string;
  description: string;
  command: string;
  cwd?: string;
  timeout?: number;
  tags?: string[];
}

export class LocalScriptRegistry {
  private readonly store: Store<LocalScriptStore>;

  constructor() {
    this.store = new Store<LocalScriptStore>({
      name: 'agent-local-scripts',
      defaults: {
        localScripts: [],
      },
    });
  }

  list(): LocalScript[] {
    return this.store.get('localScripts', []);
  }

  getById(id: string): LocalScript | null {
    const scripts = this.list();
    return scripts.find(script => script.id === id) ?? null;
  }

  create(input: CreateLocalScriptInput): LocalScript {
    const now = new Date().toISOString();
    const script: LocalScript = {
      id: randomUUID(),
      name: input.name.trim(),
      description: input.description.trim(),
      command: input.command.trim(),
      cwd: input.cwd?.trim(),
      timeout: input.timeout,
      tags: input.tags ?? [],
      createdAt: now,
      updatedAt: now,
    };
    const scripts = this.list();
    scripts.push(script);
    this.store.set('localScripts', scripts);
    return script;
  }

  update(id: string, updates: Partial<CreateLocalScriptInput>): LocalScript {
    const scripts = this.list();
    const idx = scripts.findIndex(script => script.id === id);
    if (idx < 0) {
      throw new Error(`Script not found: ${id}`);
    }

    const original = scripts[idx];
    const updated: LocalScript = {
      ...original,
      ...updates,
      name: updates.name !== undefined ? updates.name.trim() : original.name,
      description: updates.description !== undefined ? updates.description.trim() : original.description,
      command: updates.command !== undefined ? updates.command.trim() : original.command,
      cwd: updates.cwd !== undefined ? updates.cwd.trim() : original.cwd,
      updatedAt: new Date().toISOString(),
    };
    scripts[idx] = updated;
    this.store.set('localScripts', scripts);
    return updated;
  }

  delete(id: string): void {
    const scripts = this.list();
    const nextScripts = scripts.filter(script => script.id !== id);
    this.store.set('localScripts', nextScripts);
  }
}
