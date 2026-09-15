// Test-only Microsoft TraceEvent monitor. No runtime code or global policy changes.
// Persist only the fixture process tree; never store packet bodies or commands.
using System;
using System.IO;
using System.Diagnostics;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Text.Json;
using Microsoft.Diagnostics.Tracing;
using Microsoft.Diagnostics.Tracing.Parsers;
using Microsoft.Diagnostics.Tracing.Session;

public static class CCMNetworkCapture
{
    public static string Run(string python, string fixture, string directory, string executable)
    {
        string name = "CCM-OwnedNetwork-" + Guid.NewGuid().ToString("N");
        var starts = new List<Dictionary<string, object>>();
        var traffic = new List<Dictionary<string, object>>();
        var output = new Dictionary<string, object>();
        var sync = new object();
        TraceEventSession session = null;
        Thread reader = null;
        Process child = null;
        string readerError = null;
        int lost = -1;
        bool exited = false;
        int rootPid = -1;
        try
        {
            if (TraceEventSession.GetActiveSessionNames().Contains(name))
                throw new Exception("Refusing to attach to an existing trace session");
            session = new TraceEventSession(name);
            session.StopOnDispose = true;
            session.BufferSizeMB = 32;
            session.EnableKernelProvider(KernelTraceEventParser.Keywords.Process | KernelTraceEventParser.Keywords.NetworkTCPIP);
            var source = session.Source;
            source.Kernel.ProcessStart += data =>
            {
                lock (sync) starts.Add(new Dictionary<string, object> {
                    {"pid", data.ProcessID}, {"parent_pid", data.ParentID},
                    {"name", Path.GetFileName(data.ImageFileName)},
                    {"at_ms", data.TimeStampRelativeMSec}
                });
            };
            source.Kernel.All += data =>
            {
                if (data.EventName.IndexOf("TcpIp", StringComparison.OrdinalIgnoreCase) < 0 &&
                    data.EventName.IndexOf("UdpIp", StringComparison.OrdinalIgnoreCase) < 0) return;
                var fields = new Dictionary<string, object>();
                int pid = data.ProcessID;
                foreach (string key in data.PayloadNames)
                {
                    string k = key.ToLowerInvariant();
                    if (k == "pid" || k == "processid") pid = Convert.ToInt32(data.PayloadByName(key));
                    if (k == "daddr" || k == "saddr" || k == "dport" || k == "sport" || k == "size")
                        fields[k] = Convert.ToString(data.PayloadByName(key));
                }
                lock (sync) traffic.Add(new Dictionary<string, object> {
                    {"pid", pid}, {"event", data.EventName},
                    {"at_ms", data.TimeStampRelativeMSec}, {"network", fields}
                });
            };
            reader = new Thread(() => { try { source.Process(); } catch (Exception ex) { readerError = ex.GetType().Name + ": " + ex.Message; } });
            reader.IsBackground = true;
            reader.Start();
            var info = new ProcessStartInfo(python) { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = directory };
            info.ArgumentList.Add(fixture);
            info.ArgumentList.Add(directory);
            info.ArgumentList.Add(executable);
            child = Process.Start(info);
            rootPid = child.Id;
            // The fixture imports only standard local modules and waits for
            // this gate before any socket or official executable is started.
            File.WriteAllText(Path.Combine(directory, "go"), "trace_active");
            exited = child.WaitForExit(120000);
            if (!exited) throw new Exception("Owned fixture exceeded its deadline");
            output["fixture_exit_code"] = child.ExitCode;
            session.Flush();
            Thread.Sleep(1200);
            lost = session.EventsLost;
        }
        catch (Exception ex)
        {
            output["error"] = ex.GetType().Name + ": " + ex.Message;
        }
        finally
        {
            if (child != null && !exited)
            {
                try { if (!child.HasExited) child.Kill(true); child.WaitForExit(5000); } catch { }
            }
            if (session != null) session.Dispose();
            if (reader != null) reader.Join(5000);
        }
        var owned = new HashSet<int> { rootPid };
        bool changed;
        do
        {
            changed = false;
            foreach (var p in starts)
                if (owned.Contains((int)p["parent_pid"])) changed |= owned.Add((int)p["pid"]);
        } while (changed);
        // Event timestamps and process ancestry are reconciled after the stream
        // drains so cross-CPU callback delivery cannot lose child attribution.
        output["owned_processes"] = starts.Where(p => owned.Contains((int)p["pid"])).ToArray();
        output["owned_network_events"] = traffic.Where(e => owned.Contains((int)e["pid"])).ToArray();
        output["root_pid"] = rootPid;
        output["events_lost"] = lost;
        output["reader_error"] = readerError;
        output["session_name"] = name;
        output["session_removed"] = !TraceEventSession.GetActiveSessionNames().Contains(name);
        output["packet_payloads_captured"] = false;
        output["unrelated_process_details_persisted"] = false;
        output["time"] = DateTime.UtcNow.ToString("O");
        output["scope"] = "ETW Process + NetworkTCPIP (TCP/UDP IPv4/IPv6), only owned fixture ancestry retained";
        starts.Clear(); traffic.Clear();
        string json = JsonSerializer.Serialize(output, new JsonSerializerOptions { WriteIndented = true });
        File.WriteAllText(Path.Combine(directory, "etw.json"), json);
        return json;
    }
}
