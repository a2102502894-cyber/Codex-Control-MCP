param([string]$Title,[string]$ProofPath)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text=$Title; $form.Width=650; $form.Height=340; $form.StartPosition='CenterScreen'
$caption=New-Object System.Windows.Forms.Label
$caption.Text='Codex-Control-MCP controlled test window'; $caption.Left=24;$caption.Top=22;$caption.Width=570;$caption.Height=30
$inputBox=New-Object System.Windows.Forms.TextBox
$inputBox.AccessibleName='CCM Proof Input';$inputBox.Left=24;$inputBox.Top=72;$inputBox.Width=570;$inputBox.Height=40
$button=New-Object System.Windows.Forms.Button
$button.Text='Verify input';$button.AccessibleName='CCM Verify Button';$button.Left=24;$button.Top=128;$button.Width=190;$button.Height=40
$status=New-Object System.Windows.Forms.Label
$status.Text='CCM_READY';$status.AccessibleName='CCM Test Status';$status.Left=24;$status.Top=195;$status.Width=570;$status.Height=60
$button.Add_Click({$status.Text='CCM_RESULT: '+$inputBox.Text;[IO.File]::WriteAllText($ProofPath,$inputBox.Text,[Text.UTF8Encoding]::new($false))})
$form.Controls.AddRange(@($caption,$inputBox,$button,$status))
[void]$form.ShowDialog()