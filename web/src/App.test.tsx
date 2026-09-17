import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import App from './App';

describe('App', () => {
  it('renders the home nav', () => {
    render(<App />);
    expect(screen.getByRole('heading', { name: /new research task/i })).toBeTruthy();
  });
});
