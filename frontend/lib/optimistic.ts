"use client";

// Ids removed from the screen before the server has finished deleting them. Lists skip these, so a
// row stays gone even when a refresh arrives while the delete is still running (or after a page change).
const hidden = new Set<string>();

export const hideLocally = (id: string) => void hidden.add(id);
export const showAgain = (id: string) => void hidden.delete(id);
export const isHidden = (id: string) => hidden.has(id);
export const withoutHidden = <T extends { id: string }>(items: T[]) => items.filter((item) => !hidden.has(item.id));
