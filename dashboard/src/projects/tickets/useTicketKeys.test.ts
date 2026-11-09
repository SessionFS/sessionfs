import { afterEach, describe, expect, it } from 'vitest';
import { isTypingTarget, keyAction, keyScope } from './useTicketKeys';

afterEach(() => {
  document.body.innerHTML = '';
});

describe('keyAction', () => {
  it('maps the documented keys', () => {
    expect(keyAction({ key: 'j' })).toEqual({ type: 'move', delta: 1 });
    expect(keyAction({ key: 'ArrowDown' })).toEqual({ type: 'move', delta: 1 });
    expect(keyAction({ key: 'k' })).toEqual({ type: 'move', delta: -1 });
    expect(keyAction({ key: 'ArrowUp' })).toEqual({ type: 'move', delta: -1 });
    expect(keyAction({ key: 'Enter' })).toEqual({ type: 'open' });
    expect(keyAction({ key: 'o' })).toEqual({ type: 'open' });
    expect(keyAction({ key: 'Escape' })).toEqual({ type: 'escape' });
    expect(keyAction({ key: 'x' })).toEqual({ type: 'toggleSelect' });
    expect(keyAction({ key: 's' })).toEqual({ type: 'status' });
    expect(keyAction({ key: 'a' })).toEqual({ type: 'assign' });
    expect(keyAction({ key: 'c' })).toEqual({ type: 'new' });
    expect(keyAction({ key: '/' })).toEqual({ type: 'search' });
    expect(keyAction({ key: 'b' })).toEqual({ type: 'toggleLayout' });
    expect(keyAction({ key: '1' })).toEqual({ type: 'view', index: 0 });
    expect(keyAction({ key: '6' })).toEqual({ type: 'view', index: 5 });
    expect(keyAction({ key: '[' })).toEqual({ type: 'paneTab', delta: -1 });
    expect(keyAction({ key: ']' })).toEqual({ type: 'paneTab', delta: 1 });
    expect(keyAction({ key: '?' })).toEqual({ type: 'help' });
  });

  it('ignores other keys and every modifier chord', () => {
    expect(keyAction({ key: '7' })).toBeNull();
    expect(keyAction({ key: 'q' })).toBeNull();
    expect(keyAction({ key: 'J' })).toBeNull();
    expect(keyAction({ key: 'k', metaKey: true })).toBeNull();
    expect(keyAction({ key: 'c', ctrlKey: true })).toBeNull();
    expect(keyAction({ key: 'j', altKey: true })).toBeNull();
  });
});

describe('isTypingTarget', () => {
  const el = (html: string) => {
    document.body.innerHTML = html;
    return document.body.firstElementChild;
  };
  it('is true for text fields, selects and editable regions', () => {
    expect(isTypingTarget(el('<input type="text">'))).toBe(true);
    expect(isTypingTarget(el('<input type="search">'))).toBe(true);
    expect(isTypingTarget(el('<input>'))).toBe(true);
    expect(isTypingTarget(el('<textarea></textarea>'))).toBe(true);
    expect(isTypingTarget(el('<select></select>'))).toBe(true);
    expect(isTypingTarget(el('<div role="combobox"></div>'))).toBe(true);
  });
  it('is false for checkboxes, buttons and nothing', () => {
    expect(isTypingTarget(el('<input type="checkbox">'))).toBe(false);
    expect(isTypingTarget(el('<button>x</button>'))).toBe(false);
    expect(isTypingTarget(null)).toBe(false);
  });
});

describe('keyScope', () => {
  it('acts on the tab from the body or from inside the tab, not from elsewhere', () => {
    document.body.innerHTML = '<nav><a href="#">x</a></nav><section id="root"><button>row</button></section>';
    const root = document.getElementById('root');
    expect(keyScope(document.body, root)).toBe('tab');
    expect(keyScope(root!.querySelector('button'), root)).toBe('tab');
    expect(keyScope(document.querySelector('nav a'), root)).toBeNull();
  });

  it('stays off while a menu, listbox or dialog is open', () => {
    for (const overlay of ['<div role="menu"></div>', '<ul role="listbox"></ul>', '<div role="dialog"></div>']) {
      document.body.innerHTML = `<section id="root"><button>row</button>${overlay}</section>`;
      const root = document.getElementById('root');
      expect(keyScope(root!.querySelector('button'), root)).toBeNull();
    }
  });

  it('ignores overlays that are mounted but not on screen (the closed mobile nav drawer)', () => {
    document.body.innerHTML =
      '<div role="dialog" aria-modal="true" style="pointer-events: none"></div><div aria-hidden="true"><ul role="listbox"></ul></div><section id="root"><button>row</button></section>';
    const root = document.getElementById('root');
    expect(keyScope(root!.querySelector('button'), root)).toBe('tab');
  });

  it('works inside the ticket slide-over, and only there', () => {
    document.body.innerHTML =
      '<section id="root"><button id="row">row</button></section><div role="dialog" aria-modal="true"><div data-ticket-pane><button id="in">x</button></div></div>';
    const root = document.getElementById('root');
    expect(keyScope(document.getElementById('in'), root)).toBe('pane');
    expect(keyScope(document.getElementById('row'), root)).toBeNull();
  });

  it('is off while typing even with nothing open', () => {
    document.body.innerHTML = '<section id="root"><input id="q" type="search"></section>';
    expect(keyScope(document.getElementById('q'), document.getElementById('root'))).toBeNull();
  });
});
