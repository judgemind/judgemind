import { formatDate, formatLabel, groupParties } from '@/lib/display-helpers';
import { SECTION_LABEL } from '@/lib/typography';

/** Party type shared across PartyList and PartiesSection. */
export type Party = {
  id: string;
  canonicalName: string;
  partyType: string | null;
  role: string | null;
};

/** Compact party list for a single role group (plaintiffs, defendants, etc.). */
export function PartyList({
  label,
  parties,
}: {
  label: string;
  parties: Party[];
}) {
  return (
    <div>
      <h3 className={SECTION_LABEL}>
        {label}
      </h3>
      {parties.length === 0 ? (
        <p className="mt-1 text-sm text-muted-foreground">
          None listed
        </p>
      ) : (
        <ul className="mt-1 space-y-1">
          {parties.map((party) => (
            <li key={party.id} className="text-sm text-foreground">
              {party.canonicalName}
              {party.partyType && (
                <span className="ml-2 text-xs text-muted-foreground">
                  ({formatLabel(party.partyType)})
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

interface PartiesSectionProps {
  /** Raw parties array — will be grouped into plaintiffs/defendants/others internally. */
  parties: Party[];
  /** Optional filed date to display alongside parties. */
  filedAt?: string | null;
}

/**
 * Parties header section: displays plaintiffs, defendants, other parties,
 * and optional filed date in a horizontal layout.
 *
 * Used on both the case detail page and the ruling detail page.
 */
export function PartiesSection({ parties, filedAt }: PartiesSectionProps) {
  const { plaintiffs, defendants, others } = groupParties(parties);
  const hasParties = plaintiffs.length > 0 || defendants.length > 0 || others.length > 0;

  if (!hasParties && !filedAt) return null;

  return (
    <div className="flex flex-wrap gap-x-8 gap-y-4" data-testid="parties-section">
      {hasParties && (
        <>
          <PartyList label="Plaintiffs" parties={plaintiffs} />
          <PartyList label="Defendants" parties={defendants} />
          {others.length > 0 && (
            <PartyList label="Other Parties" parties={others} />
          )}
        </>
      )}
      {filedAt && (
        <div>
          <h3 className={SECTION_LABEL}>Filed Date</h3>
          <p className="mt-1 text-sm text-foreground">
            {formatDate(filedAt)}
          </p>
        </div>
      )}
    </div>
  );
}
