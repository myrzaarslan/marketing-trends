import { useState } from 'react';
import type { Collection } from '../types';
import './CollectionsBar.css';

interface Props {
  collections: Collection[];
  activeId: number | null; // null = Home
  onSelectHome: () => void;
  onSelectCollection: (id: number) => void;
  onCreate: (title: string) => void;
}

/** Horizontal rail: Home + collection chips + inline create. */
export function CollectionsBar({
  collections,
  activeId,
  onSelectHome,
  onSelectCollection,
  onCreate,
}: Props) {
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState('');

  const submit = () => {
    const t = title.trim();
    if (t) onCreate(t);
    setTitle('');
    setCreating(false);
  };

  return (
    <nav className="collections-bar" aria-label="Collections">
      <button
        type="button"
        className={`coll-chip${activeId === null ? ' coll-chip--active' : ''}`}
        onClick={onSelectHome}
      >
        ⌂ Home
      </button>

      <span className="coll-divider" />

      {collections.map((c) => (
        <button
          key={c.id}
          type="button"
          className={`coll-chip${activeId === c.id ? ' coll-chip--active' : ''}`}
          onClick={() => onSelectCollection(c.id)}
          title={c.description ?? c.title}
        >
          {c.title}
          <span className="coll-chip-count">{c.item_count}</span>
        </button>
      ))}

      {creating ? (
        <span className="coll-create">
          <input
            autoFocus
            className="coll-create-input"
            placeholder="Collection name…"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onBlur={submit}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submit();
              if (e.key === 'Escape') { setTitle(''); setCreating(false); }
            }}
          />
        </span>
      ) : (
        <button type="button" className="coll-chip coll-chip--new" onClick={() => setCreating(true)}>
          + New
        </button>
      )}
    </nav>
  );
}
