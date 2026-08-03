import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import MaintenanceSection from './MaintenanceSection';

vi.mock('../../components/Toast', () => ({
  toast: vi.fn(),
}));

describe('MaintenanceSection', () => {
  it('renders initial bloat_min_bytes, byte conversion hint, and explanation callout', () => {
    render(<MaintenanceSection settings={{ bloat_min_bytes: 2400 }} onSave={vi.fn()} />);

    const input = screen.getByPlaceholderText('2400');
    expect(input.value).toBe('2400');
    expect(screen.getByText(/2.3 KB/)).toBeInTheDocument();
    expect(screen.getByText('What is Memory Audit & Bloat Threshold?')).toBeInTheDocument();
  });

  it('shows validation error for values less than 1', async () => {
    render(<MaintenanceSection settings={{ bloat_min_bytes: 2400 }} onSave={vi.fn()} />);

    const input = screen.getByPlaceholderText('2400');
    fireEvent.change(input, { target: { value: '0' } });

    expect(screen.getByText('Threshold must be a positive integer of at least 1')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /save/i })).toBeDisabled();
  });

  it('allows selecting presets (e.g. Compact 1200 B) to update threshold', async () => {
    const onSaveMock = vi.fn().mockResolvedValue({ success: true });
    render(<MaintenanceSection settings={{ bloat_min_bytes: 2400 }} onSave={onSaveMock} />);

    const compactPreset = screen.getByText('Compact (1200 B)');
    fireEvent.click(compactPreset);

    const input = screen.getByPlaceholderText('2400');
    expect(input.value).toBe('1200');

    const saveButton = screen.getByRole('button', { name: /save/i });
    expect(saveButton).not.toBeDisabled();

    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(onSaveMock).toHaveBeenCalledWith({ bloat_min_bytes: 1200 });
    });
  });

  it('calls onSave with parsed integer when valid value is saved manually', async () => {
    const onSaveMock = vi.fn().mockResolvedValue({ success: true });
    render(<MaintenanceSection settings={{ bloat_min_bytes: 2400 }} onSave={onSaveMock} />);

    const input = screen.getByPlaceholderText('2400');
    fireEvent.change(input, { target: { value: '4800' } });

    expect(screen.getByText(/4.7 KB/)).toBeInTheDocument();

    const saveButton = screen.getByRole('button', { name: /save/i });
    expect(saveButton).not.toBeDisabled();

    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(onSaveMock).toHaveBeenCalledWith({ bloat_min_bytes: 4800 });
    });
  });

  it('does not show save button when selecting preset that matches saved value', async () => {
    render(<MaintenanceSection settings={{ bloat_min_bytes: 2400 }} onSave={vi.fn()} />);

    // Click the 2400 preset
    const standardPreset = screen.getByText(/Standard \(2400 B/i);
    fireEvent.click(standardPreset);

    // Should not show save button
    expect(screen.queryByRole('button', { name: /save/i })).not.toBeInTheDocument();
  });

  it('does not show save button when manually entering saved value', async () => {
    render(<MaintenanceSection settings={{ bloat_min_bytes: 2400 }} onSave={vi.fn()} />);

    const input = screen.getByPlaceholderText('2400');
    fireEvent.change(input, { target: { value: '1200' } });
    expect(screen.queryByRole('button', { name: /save/i })).toBeInTheDocument();

    fireEvent.change(input, { target: { value: '2400' } });
    expect(screen.queryByRole('button', { name: /save/i })).not.toBeInTheDocument();
  });
});
