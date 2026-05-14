/**
 * Tiny class-name combiner. We intentionally don't depend on `clsx` /
 * `tailwind-merge` to keep the package's runtime footprint near zero — those
 * niceties belong in the consumer app.
 */
export function cn(
  ...values: Array<string | false | null | undefined>
): string {
  return values.filter(Boolean).join(' ');
}
