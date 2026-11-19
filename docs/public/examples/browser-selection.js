// Run against the unmodified UTF-8 HTML document, not a wrapper or rendered TF view.
// The API verifies every captured text-node path against the pinned source asset.
export function captureSelection(document) {
  const selection = document.getSelection();
  if (!selection || selection.rangeCount !== 1) throw new Error("Select one passage.");
  const range = selection.getRangeAt(0);
  if (range.collapsed || range.startContainer.nodeType !== 3 || range.endContainer.nodeType !== 3)
    throw new Error("Selection endpoints must be text nodes.");
  const path = (node) => {
    const indices = [];
    while (node !== document) {
      if (!node.parentNode) throw new Error("Selection is detached.");
      indices.unshift(Array.prototype.indexOf.call(node.parentNode.childNodes, node));
      node = node.parentNode;
    }
    if (indices.length > 128) throw new Error("Document nesting exceeds the limit.");
    return indices;
  };
  const excluded = new Set(["head", "script", "style", "template", "noscript"]);
  const nodes = [];
  const visit = (node) => {
    if (node.nodeType === 1 && excluded.has(node.localName)) return;
    if (node.nodeType === 3 && node.data.length) {
      nodes.push([path(node), node.data]);
      if (nodes.length > 10000) throw new Error("Document has too many text nodes.");
    }
    for (const child of node.childNodes) visit(child);
  };
  visit(document);
  return {captured_nodes: nodes, start_path: path(range.startContainer),
    start_utf16: range.startOffset, end_path: path(range.endContainer), end_utf16: range.endOffset};
}
