const MAX_ACTIVE_DOCUMENT_IMPORTS = 4

/** Admit several documents at once so the Engine can fairly schedule their model work. */
export async function runDocumentImportBatch<Item>(
  items: readonly Item[],
  importItem: (item: Item) => Promise<void>,
): Promise<void> {
  let nextItemIndex = 0
  const importNextItem = async () => {
    while (nextItemIndex < items.length) {
      const item = items[nextItemIndex]
      nextItemIndex += 1
      await importItem(item)
    }
  }
  const workerCount = Math.min(MAX_ACTIVE_DOCUMENT_IMPORTS, items.length)
  await Promise.all(Array.from({ length: workerCount }, importNextItem))
}
