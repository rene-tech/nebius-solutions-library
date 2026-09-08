import { useId } from "react";

export interface ChartSeries {
  id: string;
  label: string;
  points: Array<{ at: string; value: number | null }>;
}
export interface TimeSeriesChartProps {
  title: string;
  unit: string;
  series: ChartSeries[];
  source: string;
  aggregation: string;
  state: "available" | "partial" | "unavailable";
  reason: string | null;
  summary: { average: number | null; maximum: number | null };
}

const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
export function chartValue(value: number | null, unit: string) {
  return value === null || !Number.isFinite(value)
    ? "—"
    : `${number.format(value)} ${unit}`.trim();
}

/** Null samples break the path: unknown intervals must never appear as zeros. */
export function chartSegments(
  series: ChartSeries,
  x: (time: number) => number,
  y: (value: number) => number,
) {
  const segments: string[] = [];
  let segment: string[] = [];
  for (const point of series.points) {
    const at = Date.parse(point.at);
    if (
      point.value === null ||
      !Number.isFinite(point.value) ||
      !Number.isFinite(at)
    ) {
      if (segment.length) segments.push(segment.join(" "));
      segment = [];
    } else segment.push(`${x(at)},${y(point.value)}`);
  }
  if (segment.length) segments.push(segment.join(" "));
  return segments;
}

export function TimeSeriesChart(props: TimeSeriesChartProps) {
  const titleId = useId();
  const points = props.series
    .flatMap((series) => series.points)
    .filter((point) => Number.isFinite(Date.parse(point.at)));
  const values = points.flatMap((point) =>
    point.value !== null && Number.isFinite(point.value) ? [point.value] : [],
  );
  const times = points.map((point) => Date.parse(point.at));
  const start = Math.min(...times),
    end = Math.max(...times);
  const floor = Math.min(0, ...values),
    ceiling = Math.max(1, ...values);
  const x = (time: number) =>
    48 + ((time - start) / Math.max(1, end - start)) * 568;
  const y = (value: number) =>
    168 - ((value - floor) / (ceiling - floor)) * 138;
  const available = props.state !== "unavailable" && values.length > 0;
  return (
    <section className="panel time-chart" aria-labelledby={titleId}>
      <div className="section-heading">
        <h3 id={titleId}>{props.title}</h3>
        <span className="eyebrow">{props.unit}</span>
      </div>
      <div className="chart-summary">
        <span>
          Average{" "}
          <strong>{chartValue(props.summary.average, props.unit)}</strong>
        </span>
        <span>
          Peak <strong>{chartValue(props.summary.maximum, props.unit)}</strong>
        </span>
      </div>
      {available ? (
        <svg
          className="chart-svg"
          viewBox="0 0 640 204"
          role="img"
          aria-label={`${props.title}, ${props.unit}; gaps mean no observation`}
        >
          {[0, 0.5, 1].map((fraction) => {
            const value = floor + (ceiling - floor) * fraction;
            return (
              <g key={fraction}>
                <line
                  x1="48"
                  x2="616"
                  y1={y(value)}
                  y2={y(value)}
                  className="chart-grid"
                />
                <text x="40" y={y(value) + 4} textAnchor="end">
                  {number.format(value)}
                </text>
              </g>
            );
          })}
          {props.series.map((series, index) => (
            <g
              key={series.id}
              className={`chart-line chart-line--${index % 4}`}
            >
              {chartSegments(series, x, y).map((segment, i) => (
                <polyline key={i} points={segment} />
              ))}
              {series.points
                .filter(
                  (point) =>
                    point.value !== null &&
                    Number.isFinite(point.value) &&
                    Number.isFinite(Date.parse(point.at)),
                )
                .map((point, i) => (
                  <circle
                    key={i}
                    cx={x(Date.parse(point.at))}
                    cy={y(point.value!)}
                    r="2"
                  >
                    <title>
                      {series.label} · {point.at} ·{" "}
                      {chartValue(point.value, props.unit)}
                    </title>
                  </circle>
                ))}
            </g>
          ))}
          <text x="48" y="194">
            {new Date(start).toLocaleTimeString()}
          </text>
          <text x="616" y="194" textAnchor="end">
            {new Date(end).toLocaleTimeString()}
          </text>
        </svg>
      ) : (
        <div className="chart-empty" role="status">
          {props.reason ?? "No observations in this window."}
        </div>
      )}
      <div className="chart-legend">
        {props.series.map((series, index) => (
          <span key={series.id} className={`chart-key chart-key--${index % 4}`}>
            {series.label}
          </span>
        ))}
      </div>
      <p className="supporting-copy">
        {props.aggregation} · {props.source}
        {props.state === "partial"
          ? ` · Partial: ${props.reason ?? "some samples unavailable"}`
          : ""}
      </p>
      {available ? (
        <details className="chart-data">
          <summary>View observed samples</summary>
          <div className="table-frame">
            <table className="resource-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Series</th>
                  <th>Value</th>
                </tr>
              </thead>
              <tbody>
                {props.series.flatMap((series) =>
                  series.points.map((point, index) => (
                    <tr key={`${series.id}-${index}`}>
                      <td>{point.at}</td>
                      <td>{series.label}</td>
                      <td>{chartValue(point.value, props.unit)}</td>
                    </tr>
                  )),
                )}
              </tbody>
            </table>
          </div>
        </details>
      ) : null}
    </section>
  );
}
