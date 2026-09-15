using System;
using System.Drawing;
using System.IO;
using System.Text;
using System.Windows.Forms;

// A disposable test app. It accepts no commands and writes only its supplied
// proof file when a human/official GUI action clicks Verify.
class GuiFixture {
  [STAThread] static void Main(string[] args) {
    if(args.Length != 2) return;
    Application.EnableVisualStyles();
    var f=new Form { Text=args[0], Width=680, Height=500, StartPosition=FormStartPosition.CenterScreen };
    var label=new Label { Text="Codex-Control-MCP GUI acceptance", Left=24, Top=20, Width=600, Height=30 };
    var input=new TextBox { AccessibleName="CCM Proof Input", Left=24, Top=65, Width=600 };
    var button=new Button { Text="Verify input", AccessibleName="CCM Verify Button", Left=24, Top=105, Width=180, Height=38 };
    var status=new Label { Text="CCM_READY", AccessibleName="CCM Test Status", Left=24, Top=158, Width=600, Height=38 };
    var scroll=new TextBox { AccessibleName="CCM Scroll Area", Left=24, Top=210, Width=600, Height=200, Multiline=true, ScrollBars=ScrollBars.Vertical, ReadOnly=true };
    for(int i=1;i<=100;i++) scroll.AppendText("CCM scroll line "+i+Environment.NewLine);
    scroll.SelectionStart=0; scroll.ScrollToCaret();
    button.Click += (s,e) => { status.Text="CCM_RESULT: "+input.Text; File.WriteAllText(args[1], input.Text, new UTF8Encoding(false)); };
    f.Controls.AddRange(new Control[]{label,input,button,status,scroll});
    Application.Run(f);
  }
}
