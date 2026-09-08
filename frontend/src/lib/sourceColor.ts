/**
 * A stable colour per publisher.
 *
 * This is the one place the design spends boldness (design-council §Anti-averaging),
 * and it is functional rather than decorative: on an aggregator the reader's
 * constant question is "who is telling me this?", and after a few visits a
 * consistent hue answers it before the name is read. That is a device only an
 * aggregator needs — a single publisher's own site would have no use for it.
 *
 * Derived from the name rather than the row id so the colour is stable across
 * reseeds and database resets, and identical between server and client render.
 */
export function sourceHue(name: string): number {
  // FNV-1a. Small, deterministic, and well-spread for short strings — which
  // matters because two publishers landing on neighbouring hues defeats the
  // entire point of the device.
  let hash = 0x811c9dc5;
  for (let i = 0; i < name.length; i++) {
    hash ^= name.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }

  // Skirt the 55–85° band: yellow-greens go muddy at the saturation and
  // lightness used here, in both themes.
  const raw = Math.abs(hash) % 330;
  return raw > 55 ? raw + 30 : raw;
}
