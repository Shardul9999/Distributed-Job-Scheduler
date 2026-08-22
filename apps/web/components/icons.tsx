// Inline SVG icon set.
//
// Hand-rolled rather than pulled from a package: six icons do not justify a
// dependency, and inlining means no icon-font request, no flash of unstyled
// glyph, and no bundle weight beyond the markup itself. Every icon is drawn on
// the same 24x24 grid with a 1.75 stroke so they sit together optically.
//
// These replace Unicode text glyphs (◆ ≡ ☰ ⚙ ◷ ⚠), which rendered differently
// on every platform and gave two different nav items the same shape.

type IconProps = {
  className?: string;
};

function Svg({
  className = "h-4 w-4",
  children,
}: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

/** Overview -- a dashboard grid. */
export function IconOverview(p: IconProps) {
  return (
    <Svg {...p}>
      <rect x="3" y="3" width="7" height="8" rx="1.5" />
      <rect x="14" y="3" width="7" height="5" rx="1.5" />
      <rect x="3" y="14" width="7" height="7" rx="1.5" />
      <rect x="14" y="11" width="7" height="10" rx="1.5" />
    </Svg>
  );
}

/** Queues -- stacked layers waiting to be drained. */
export function IconQueues(p: IconProps) {
  return (
    <Svg {...p}>
      <path d="M12 3 3 7.5l9 4.5 9-4.5L12 3Z" />
      <path d="M3 12.5 12 17l9-4.5" />
      <path d="M3 17 12 21.5 21 17" />
    </Svg>
  );
}

/** Job Explorer -- a list under a magnifier. */
export function IconJobs(p: IconProps) {
  return (
    <Svg {...p}>
      <path d="M4 6h10M4 11h6M4 16h5" />
      <circle cx="16.5" cy="15.5" r="3.5" />
      <path d="m19.2 18.2 2.3 2.3" />
    </Svg>
  );
}

/** Workers -- machines in a fleet. */
export function IconWorkers(p: IconProps) {
  return (
    <Svg {...p}>
      <rect x="3" y="4" width="18" height="7" rx="1.75" />
      <rect x="3" y="14" width="18" height="6" rx="1.75" />
      <path d="M7 7.5h.01M7 17h.01" />
    </Svg>
  );
}

/** Schedules -- a clock, because cron is time. */
export function IconSchedules(p: IconProps) {
  return (
    <Svg {...p}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5.2l3.2 2" />
    </Svg>
  );
}

/** Dead letters -- an envelope that did not arrive. */
export function IconDeadLetters(p: IconProps) {
  return (
    <Svg {...p}>
      <rect x="2.5" y="5" width="19" height="14" rx="2" />
      <path d="m2.5 7 9.5 6.5L21.5 7" />
      <path d="m15.5 15.5 5 5m0-5-5 5" />
    </Svg>
  );
}

/** Sign out. */
export function IconSignOut(p: IconProps) {
  return (
    <Svg {...p}>
      <path d="M15 4h3.5A1.5 1.5 0 0 1 20 5.5v13a1.5 1.5 0 0 1-1.5 1.5H15" />
      <path d="M10 8 6 12l4 4" />
      <path d="M6 12h9" />
    </Svg>
  );
}

/** Chevron, for the project switcher. */
export function IconChevron(p: IconProps) {
  return (
    <Svg {...p}>
      <path d="m6 9 6 6 6-6" />
    </Svg>
  );
}

/** Light mode. */
export function IconSun(p: IconProps) {
  return (
    <Svg {...p}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </Svg>
  );
}

/** Dark mode. */
export function IconMoon(p: IconProps) {
  return (
    <Svg {...p}>
      <path d="M20 13.5A8.2 8.2 0 0 1 10.5 4a8.5 8.5 0 1 0 9.5 9.5Z" />
    </Svg>
  );
}

/** Follow the operating system. */
export function IconSystem(p: IconProps) {
  return (
    <Svg {...p}>
      <rect x="2.5" y="4" width="19" height="12" rx="2" />
      <path d="M9 20h6M12 16v4" />
    </Svg>
  );
}
