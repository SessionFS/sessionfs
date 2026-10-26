import { friendlyModelName } from '../../utils/models';
import { Badge } from '../../components/ui';

/** Friendly model name chip for session rows and cards.
 *
 *  Rows and cards are made clickable by their title button's `::after`
 *  overlay, so the chip is `pointer-events-none`: a click on it falls through
 *  to the overlay instead of hitting a dead spot, and it adds no nested
 *  interactive element. The raw id (e.g. `gpt-5-20250127`, shortened to
 *  `gpt-5` on screen) is kept as visually hidden text for screen readers. */
export function ModelChip({ modelId }: { modelId: string }) {
  const friendly = friendlyModelName(modelId);
  return (
    <span className="inline-flex pointer-events-none">
      <span aria-hidden={friendly !== modelId ? true : undefined}>
        <Badge label={friendly} size="sm" />
      </span>
      {friendly !== modelId && <span className="sr-only">Model {modelId}</span>}
    </span>
  );
}
