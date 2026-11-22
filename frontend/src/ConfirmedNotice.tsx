import { BadgeCheck } from "lucide-react";
import { useState } from "react";

const SHOWN = 15;

/** Lists defects that already have a confirmed RCA (ingested into or reviewed in the
 *  knowledge base), so they are neither analyzed nor ingested again. */
export function ConfirmedNotice({ ids, message }: { ids: number[]; message: string }) {
  const [expanded, setExpanded] = useState(false);
  if (!ids || ids.length === 0) return null;
  const shown = expanded ? ids : ids.slice(0, SHOWN);
  return (
    <div className="confirmed-notice" role="status">
      <BadgeCheck aria-hidden="true" size={18} />
      <div>
        <p>
          <b>
            {ids.length} defect{ids.length === 1 ? "" : "s"} already {ids.length === 1 ? "has" : "have"} a confirmed RCA.
          </b>{" "}
          {message}
        </p>
        <p className="confirmed-ids">
          {shown.map((id) => (
            <span className="id-chip" key={id}>
              #{id}
            </span>
          ))}
          {ids.length > SHOWN && (
            <button className="text-button" onClick={() => setExpanded(!expanded)} type="button">
              {expanded ? "Show fewer" : `+${ids.length - SHOWN} more`}
            </button>
          )}
        </p>
      </div>
    </div>
  );
}
