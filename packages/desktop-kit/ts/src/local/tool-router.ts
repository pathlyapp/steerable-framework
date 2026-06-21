import type { LocalExecutor } from './local-executor.js';

export interface ToolSpec {
  name: string;
  description: string;
  jsonSchema: Record<string, unknown>;
  mode: 'read' | 'destructive' | 'local' | 'external' | 'safe_write';
}

export class LocalToolRouter {
  constructor(private readonly executor: LocalExecutor) {}

  listTools(): ToolSpec[] {
    return [
      {
        name: 'local_exec_shell',
        description: 'Execute a local shell command in the background',
        jsonSchema: {
          type: 'object',
          properties: {
            command: { type: 'string', description: 'The shell command to run' },
            cwd: { type: 'string', description: 'Working directory path (optional)' },
            timeoutMs: { type: 'number', description: 'Timeout in milliseconds (optional)' },
          },
          required: ['command'],
        },
        mode: 'destructive',
      },
      {
        name: 'local_read_file',
        description: 'Read a text file from the local file system',
        jsonSchema: {
          type: 'object',
          properties: {
            path: { type: 'string', description: 'Absolute or relative file path to read' },
          },
          required: ['path'],
        },
        mode: 'read',
      },
      {
        name: 'local_write_file',
        description: 'Write content to a file on the local file system',
        jsonSchema: {
          type: 'object',
          properties: {
            path: { type: 'string', description: 'Destination file path' },
            content: { type: 'string', description: 'Text contents to write' },
            createDirs: { type: 'boolean', description: 'Automatically create parent directories' },
          },
          required: ['path', 'content'],
        },
        mode: 'destructive',
      },
      {
        name: 'local_open_path',
        description: 'Open a local folder, file path, or external URL using default system handler',
        jsonSchema: {
          type: 'object',
          properties: {
            target: { type: 'string', description: 'The file path, folder path, or HTTP/HTTPS link to open' },
          },
          required: ['target'],
        },
        mode: 'local',
      },
    ];
  }

  async execute(name: string, args: Record<string, unknown>): Promise<any> {
    switch (name) {
      case 'local_exec_shell':
        return await this.executor.executeShell({
          command: String(args.command || ''),
          cwd: typeof args.cwd === 'string' ? args.cwd : undefined,
          timeoutMs: typeof args.timeoutMs === 'number' ? args.timeoutMs : undefined,
        });
      case 'local_read_file':
        return await this.executor.readLocalFile({
          path: String(args.path || ''),
        });
      case 'local_write_file':
        return await this.executor.writeLocalFile({
          path: String(args.path || ''),
          content: String(args.content || ''),
          createDirs: Boolean(args.createDirs),
        });
      case 'local_open_path':
        return await this.executor.openLocalTarget({
          target: String(args.target || ''),
        });
      default:
        throw new Error(`Tool ${name} not found in LocalToolRouter`);
    }
  }
}
