/**
 * Product-neutral authenticated identity attached to one local-backend request.
 */
export interface Principal {
  id: string;
  tenantId: string;
  displayName: string;
  email: string | null;
  roles: readonly string[];
  isAdmin: boolean;
}

export type AuthDecision =
  | { ok: true; principal: Principal }
  | { ok: false; status: 401 | 403; body?: unknown };

/**
 * Authentication extension point for BS deployments.
 *
 * Implementations own all interpretation and validation of request headers,
 * including cookies and any identity assertions added by a trusted proxy.
 */
export interface AuthProvider {
  readonly id: string;
  authenticate(
    headers: Readonly<Record<string, string | string[] | undefined>>,
  ): Promise<AuthDecision>;
  describeSelf(principal: Principal): Promise<unknown>;
}

let activeProvider: AuthProvider | null = null;

/**
 * Registers the process-wide provider before the BS host starts.
 *
 * @returns A disposer that removes this provider if it is still active.
 */
export function registerAuthProvider(provider: AuthProvider): () => void {
  if (activeProvider) {
    throw new Error(
      `[auth] provider already registered: ${activeProvider.id}`,
    );
  }
  activeProvider = provider;
  return () => {
    if (activeProvider === provider) activeProvider = null;
  };
}

/** Returns the provider registered by the product composition root, if any. */
export function getAuthProvider(): AuthProvider | null {
  return activeProvider;
}
