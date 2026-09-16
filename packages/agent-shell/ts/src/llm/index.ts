import { localStore, type LlmSettings } from '../storage/index.js';
import { sidecarWireProvider } from '../storage/llm-settings.js';
import { OllamaProvider } from './ollama.js';
import { OpenAICompatProvider } from './openai-compat.js';
import { SidecarLlmProvider } from './sidecar-provider.js';
import { getSidecarSupervisor, isSidecarEnabled, setSidecarSupervisor, setSidecarSupervisorPending, whenSidecarSupervisor } from '../sidecar/handle.js';

// The sidecar handle lives in the dependency-light `sidecar/handle.ts`; it is
// re-exported here so existing callers (`main.ts`, `router.ts`) keep one
// import site.
export { getSidecarSupervisor, setSidecarSupervisor, setSidecarSupervisorPending, whenSidecarSupervisor };
import type {
  LlmGenerateRequest,
  LlmGenerateResult,
  LlmGenerateStreamRequest,
  LlmProvider,
} from './types.js';

export class LlmService {
  private provider: LlmProvider | null = null;
  private cachedConfigSignature = '';

  async listModels(): Promise<string[]> {
    const provider = this.getProvider();
    return provider.listModels();
  }

  async generate(request: Omit<LlmGenerateRequest, 'model'> & { model?: string }): Promise<LlmGenerateResult> {
    const settings = this.getSettings();
    const provider = this.getProvider();
    // No TS-side retry: the sidecar path retries via CoreLoop RetryHooks, and
    // the in-proc fallback only serves auxiliary calls (e.g. title gen).
    return provider.generate({
      ...request,
      model: request.model || settings.model,
      temperature: request.temperature ?? settings.temperature,
    });
  }

  /**
   * 流式生成；provider 不支持时降级到一次性 generate（在结尾整体回调一次 onToken）。
   */
  async generateStream(
    request: Omit<LlmGenerateStreamRequest, 'model'> & { model?: string }
  ): Promise<LlmGenerateResult> {
    const settings = this.getSettings();
    const provider = this.getProvider();
    const merged: LlmGenerateStreamRequest = {
      ...request,
      model: request.model || settings.model,
      temperature: request.temperature ?? settings.temperature,
    };
    // 当本轮带 tools 时，强制走非流式：Ollama 在 stream + tools 组合下，
    // 部分模型版本会丢 tool_calls。我们宁可牺牲 tool 回合的打字机效果，
    // 也要保证工具调用稳定到达。纯文本回合（无 tools）仍走流式。
    const hasTools = Array.isArray(merged.tools) && merged.tools.length > 0;
    if (provider.generateStream && !hasTools) {
      return provider.generateStream(merged);
    }
    const result = await provider.generate(merged);
    if (result.content) {
      request.callbacks?.onToken?.(result.content);
    }
    return result;
  }

  getSettings(): LlmSettings {
    return localStore.getLlmSettings() || {
      provider: 'ollama',
      model: 'llama3.1:8b',
      baseUrl: 'http://127.0.0.1:11434',
      temperature: 0.3,
    };
  }

  setSettings(next: LlmSettings): LlmSettings {
    const saved = localStore.setLlmSettings(next);
    this.provider = null;
    this.cachedConfigSignature = '';
    return saved;
  }

  private getProvider(): LlmProvider {
    const settings = this.getSettings();
    const sidecarOn = isSidecarEnabled();
    const signature = `${sidecarOn ? 'sidecar' : 'in-proc'}:${JSON.stringify(settings)}`;
    if (this.provider && this.cachedConfigSignature === signature) {
      return this.provider;
    }

    const supervisor = getSidecarSupervisor();
    if (sidecarOn && supervisor) {
      this.provider = new SidecarLlmProvider({
        supervisor,
        provider: sidecarWireProvider(settings.provider),
        model: settings.model,
        baseUrl: settings.baseUrl,
        apiKey: settings.apiKey,
        temperature: settings.temperature,
      });
    } else if (settings.provider === 'ollama') {
      this.provider = new OllamaProvider({
        baseUrl: settings.baseUrl || 'http://127.0.0.1:11434',
        model: settings.model,
        temperature: settings.temperature,
      });
    } else {
      this.provider = new OpenAICompatProvider({
        baseUrl: settings.baseUrl || 'https://api.openai.com',
        apiKey: settings.apiKey,
        model: settings.model,
        temperature: settings.temperature,
      });
    }
    this.cachedConfigSignature = signature;
    return this.provider;
  }
}

export const llmService = new LlmService();
export * from './types.js';
