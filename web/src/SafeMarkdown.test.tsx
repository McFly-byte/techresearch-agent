import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SafeMarkdown } from './App';

describe('SafeMarkdown', () => {
  it('renders headings and paragraphs without HTML injection', () => {
    render(<SafeMarkdown source={'# Title\n\nParagraph <script>alert(1)</script>'} />);
    expect(screen.getByRole('heading', { name: 'Title' })).toBeTruthy();
    // The <script> tag must be escaped, not executed.
    expect(document.body.textContent).toContain('alert(1)');
    expect(document.querySelector('script')).toBeNull();
  });

  it('turns [c1] into a clickable anchor', () => {
    render(<SafeMarkdown source={'Fact about X [c1].'} />);
    const a = document.querySelector('a[href="#c1"]');
    expect(a).toBeTruthy();
    expect(a?.textContent).toBe('[c1]');
  });

  it('turns underscore-style citations [c_task_1_1] into clickable anchors (E2E regression)', () => {
    render(<SafeMarkdown source={'LangGraph is stateful [c_task_1_1] and [c_task_2_1].'} />);
    const a1 = document.querySelector('a[href="#c_task_1_1"]');
    const a2 = document.querySelector('a[href="#c_task_2_1"]');
    expect(a1).toBeTruthy();
    expect(a1?.textContent).toBe('[c_task_1_1]');
    expect(a2).toBeTruthy();
    expect(a2?.textContent).toBe('[c_task_2_1]');
  });
});
