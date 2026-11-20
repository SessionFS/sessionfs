import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { enterpriseRoute } from './ee';

describe('Enterprise route seam', () => {
  it('is a valid <Routes> child, so the app renders in a Community build', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route path="/" element={<p>home</p>} />
          {enterpriseRoute}
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText('home')).toBeInTheDocument();
  });

  it('adds no route in a Community build', () => {
    expect(enterpriseRoute).toBeNull();
  });
});
