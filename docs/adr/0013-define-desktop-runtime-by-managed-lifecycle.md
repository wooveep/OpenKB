# Define the Desktop Runtime by one managed application lifetime

The Portable Desktop Package exposes one executable entry point, but the
Desktop Runtime may use packaged UI and worker child processes when required
by the selected framework or document pipeline. It must not open a local HTTP
service or register a separately managed backend, and every child process must
terminate with the application. On Windows a dedicated EngineSupervisor places
the shell, Engine, and inherited workers in a Job Object configured to kill the
tree when its final handle closes; this favors framework choice and worker
isolation without exposing process topology or orphan cleanup to the user.
