import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import MaintenanceSection from './MaintenanceSection';

vi.mock('../../components/Toast', () => ({
  toast: vi.fn(),
}));

describe('MaintenanceSection', () => {
  it('renders initial bloat_min_bytes and byte conversion hint', () => {
    render(<MaintenanceSection settings={{ bloat_min_bytes: 2048 }} onSave={vi.fn()} />);

    const input = screen.getByPlaceholderText('2048');
    expect(input.value).toBe('2048');
    expect(screen.getByText('≈ 2.0 KB')).toBeInTheDocument();
  });

  it('shows validation error for values less than 1', async () => {
    render(<MaintenanceSection settings={{ bloat_min_bytes: 2048 }} onSave={vi.fn()} />);

    const input = screen.getByPlaceholderText('2048');
    fireEvent.change(input, { target: { value: '0' } });

    expect(screen.getByText('Threshold must be a positive integer of at least 1')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /save/i })).toBeDisabled();
  });

  it('calls onSave with parsed integer when valid value is saved', async () => {
    const onSaveMock = vi.fn().mockResolvedValue({ success: true });
    render(<MaintenanceSection settings={{ bloat_min_bytes: 2048 }} onSave={onSaveMock} />);

    const input = screen.getByPlaceholderText('2048');
    fireEvent.change(input, { target: { value: '4096' } });

    expect(screen.getByText('≈ 4.0 KB')).toBeInTheDocument();

    const saveButton = screen.getByRole('button', { name: /save/i });
    expect(saveButton).not.toBeDisabled();

    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(onSaveMock).toHaveBeenCalledWith({ bloat_min_bytes: 4096 });
    });
  });
});
