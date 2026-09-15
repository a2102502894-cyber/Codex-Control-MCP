// Windows GUI-subsystem lifecycle host. It launches only this release's bridge
// with CREATE_NO_WINDOW; it does not execute MCP commands or replace Codex.
using System;
using System.Diagnostics;
using System.IO;
using System.Text;

internal static class WindowsHost
{
    private static string Quote(string value)
    {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char ch in value)
        {
            if (ch == '\\') { slashes++; continue; }
            if (ch == '"')
            {
                result.Append('\\', slashes * 2 + 1);
                result.Append(ch);
            }
            else
            {
                result.Append('\\', slashes);
                result.Append(ch);
            }
            slashes = 0;
        }
        result.Append('\\', slashes * 2);
        result.Append('"');
        return result.ToString();
    }

    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length < 3 || args[0] != "--home" ||
            (args[2] != "serve" && args[2] != "tunnel")) return 2;
        try
        {
            string executable = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "Codex-Control-MCP.exe");
            if (!File.Exists(executable)) return 3;
            var arguments = new StringBuilder();
            foreach (string arg in args)
            {
                if (arguments.Length > 0) arguments.Append(' ');
                arguments.Append(Quote(arg));
            }
            var info = new ProcessStartInfo(executable, arguments.ToString())
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                WorkingDirectory = Environment.CurrentDirectory,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            using (var process = new Process())
            {
                process.StartInfo = info;
                process.OutputDataReceived += delegate { };
                process.ErrorDataReceived += delegate { };
                process.Start();
                process.StandardInput.Close();
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                process.WaitForExit();
                return process.ExitCode;
            }
        }
        catch { return 4; }
    }
}
