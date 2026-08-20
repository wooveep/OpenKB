# Run background work in the desktop process

The Desktop Runtime hosts its own background task scheduler and does not
require a persistent local web backend. Closing the main window while work is
active minimizes the application to the system tray so tasks continue; an
explicit application exit performs a safe stop and makes unfinished work
recoverable on the next launch. This keeps the desktop product self-contained
while preserving durable recovery.
