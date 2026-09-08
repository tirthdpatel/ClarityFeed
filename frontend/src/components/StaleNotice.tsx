/**
 * "Ingestion has not run recently" is a state the reader should be told about
 * (V3 F5). Without it a stalled pipeline looks like a slow news day, and the
 * first symptom of a disabled GitHub Actions schedule is a site that quietly
 * stopped updating a fortnight ago.
 *
 * The threshold is deliberately generous: ingestion runs hourly, so six hours
 * is unambiguous rather than a warning that cries wolf on one skipped run.
 */
const STALE_AFTER_HOURS = 6;

export function StaleNotice({ latestPublishedAt }: { latestPublishedAt: string | null }) {
  if (!latestPublishedAt) return null;

  const ageHours = (Date.now() - new Date(latestPublishedAt).getTime()) / 3_600_000;
  if (!Number.isFinite(ageHours) || ageHours < STALE_AFTER_HOURS) return null;

  const rounded = Math.floor(ageHours);
  return (
    <p className="stale" role="status">
      The newest story here is about {rounded} hours old, so the feed may not be
      updating. It refreshes hourly when everything is working.
    </p>
  );
}
