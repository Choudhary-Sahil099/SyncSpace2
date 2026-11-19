import React from "react";

export type RemoteCursor = {
  connection_id: string;
  username: string;
  position: number; // Unicode code points
  anchor: number;
  revision: number;
  seen: number;
};

const COLORS = [
  "#4f46e5",
  "#db2777",
  "#059669",
  "#d97706",
  "#0284c7",
  "#9333ea",
];
export function cursorColor(id: string): string {
  let hash = 0;
  for (let i = 0; i < id.length; i++)
    hash = (hash * 31 + id.charCodeAt(i)) >>> 0;
  return COLORS[hash % COLORS.length];
}

function utf16Index(text: string, pointIndex: number): number {
  return Array.from(text).slice(0, Math.max(0, pointIndex)).join("").length;
}

export function RemoteCursorOverlay({
  text,
  cursors,
  scrollTop,
  scrollLeft,
}: {
  text: string;
  cursors: RemoteCursor[];
  scrollTop: number;
  scrollLeft: number;
}) {
  return (
    <div className="remote-overlay-viewport" aria-hidden="true">
      {cursors.map((c) => {
        const start = utf16Index(text, Math.min(c.anchor, c.position));

        const end = utf16Index(text, Math.max(c.anchor, c.position));

        const caretOffset = utf16Index(text, c.position);
        const color = cursorColor(c.connection_id);

        const hasSelection = start !== end;

        const before = text.slice(0, start);
        const selected = text.slice(start, end);
        const after = text.slice(end);

        return (
          <div
            key={c.connection_id}
            className="remote-mirror"
            style={{
              transform: `translate(${-scrollLeft}px, ${-scrollTop}px)`,
            }}
          >
            {hasSelection ? (
              <>
                <span>{before}</span>

                <span
                  className="remote-selection"
                  style={{
                    backgroundColor: `${color}40`,
                  }}
                >
                  {selected}
                </span>

                <span>{after}</span>
              </>
            ) : (
              <>
                <span>{text.slice(0, caretOffset)}</span>

                <span className="remote-caret" style={{ borderColor: color }}>
                  <span
                    className="remote-caret-label"
                    style={{ backgroundColor: color }}
                  >
                    {c.username}
                  </span>
                </span>

                <span>{text.slice(caretOffset)}</span>
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}
