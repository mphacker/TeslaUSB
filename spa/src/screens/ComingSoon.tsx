import { Icon } from "../components/Icon";

/**
 * A small, parity-styled placeholder for screens that land in later 5.3 lanes
 * (analytics, cloud, settings, events). Reuses the existing `settings-section`
 * + `device-status-card` primitives so the chrome stays consistent while the
 * real screen is pending. Not a parity baseline target — intentionally minimal.
 */
export function ComingSoon({ title }: { title: string }) {
  return (
    <div class="container" data-screen="coming-soon" data-title={title}>
      <div class="device-status-card device-status-unknown">
        <div class="device-status-header">
          <span class="status-dot status-unknown" />
          <div class="device-status-info">
            <strong>{title}</strong>
            <p>This screen is coming soon — it lands in a later parity lane.</p>
          </div>
        </div>
      </div>
      <div class="settings-section" id="coming-soon-section">
        <div class="section-content">
          <p class="health-value" data-testid="coming-soon">
            <Icon name="clock" /> {title} is not available yet.
          </p>
        </div>
      </div>
    </div>
  );
}
