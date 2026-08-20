# Connect the Desktop Shell and Python Engine over private stdio

The Desktop Shell supervises one persistent Python Engine and communicates
through versioned, length-prefixed JSON-RPC frames over the child process's
standard input and output, with diagnostics isolated on standard error. The
protocol supports commands, cancellation, streaming events, and version
handshake without a listening port; large files and Source Images instead use
an allowlisted local resource path, favoring a small private attack surface and
parent-controlled lifecycle over local HTTP or an independently discoverable
IPC service.
