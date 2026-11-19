"""GTK styles shared by the desktop workspace pages."""

WORKSPACE_CSS = """
window, .workspace-root, .content, .content-scroll viewport {
    background-color: #f7f7f2; color: #202f29;
}
.workspace-root { font-family: "Noto Sans CJK SC", sans-serif; font-size: 13px; }
.sidebar { background: #f1f2e9; border-right: 1px solid #d6dbce; padding: 25px 13px 18px; }
.sidebar.compact { padding: 18px 7px 12px; }
.brand-row { padding: 0 6px 12px; }
.brand-mark { color: #fbfaf0; background: #254d3b; border-radius: 11px; padding: 10px; }
.brand { color: #233c2c; font-family: "Noto Serif CJK SC", serif; font-size: 23px; font-weight: bold; }
.nav-caption { color: #5e6d60; padding: 17px 10px 5px; font-size: 12px; }
.nav-button { border: 1px solid transparent; background: transparent; color: #3a4c3d;
    box-shadow: none; border-radius: 8px; padding: 8px 10px; min-height: 21px; }
.nav-button:hover { background: #e5eadf; }
.nav-button:checked { color: #163e2b; background: #d8e6d4; border-color: #cbdcc5; font-weight: bold; }
.nav-button label { color: inherit; }
.sidebar separator { background: #d4d9cb; min-height: 1px; }
.version { color: #637164; font-size: 11px; padding: 0 9px; }
.content { padding: 22px 27px; }
.tight-content .content { padding: 16px 16px; }
.title { color: #1c3929; font-family: "Noto Serif CJK SC", serif; font-size: 29px; font-weight: bold; }
.page-heading separator { background: #cdd6c9; min-height: 1px; }
.subtitle { color: #57675e; }
.result-success { color: #24603f; }
.result-error { color: #9d382e; }
.source-link, .source-link label { color: #2a674c; }
.card { background: #fffefa; border: 1px solid #d9ded3; border-radius: 12px; padding: 15px 18px; }
.section-title { color: #263f30; font-weight: bold; font-size: 17px; }
button { background-image: none; background-color: #fffefa; color: #253e30; border: 1px solid #c4cec2;
    padding: 6px 11px; border-radius: 7px; box-shadow: none; text-shadow: none; }
button:hover { background: #eef3e9; border-color: #94ad99; }
button:active { background: #dce9db; }
button:focus, entry:focus { border-color: #38805a; }
button.primary { background: #285740; color: #ffffff; border-color: #285740; font-weight: bold; }
button.primary:hover { background: #326b4d; }
button.primary:active { background: #1e4432; }
button:disabled, button.primary:disabled { background: #f1f2ec; color: #68756c; border-color: #d7dcd3; }
.status-strip { background: #f3f5ee; color: #53695a; border-top: 1px solid #dbe2d7; padding: 7px 18px; }
.status { color: #475b4f; font-size: 12px; }
textview, textview text { background: #ffffff; color: #23392d; }
entry { background: #ffffff; color: #23392d; padding: 6px 9px; border: 1px solid #c7d2c6; border-radius: 6px; }
treeview.view { background: #ffffff; color: #273e30; }
treeview.view:selected { background: #d9eadc; color: #183c28; }
treeview.view header button { background: #eef2e9; color: #39503e; border-radius: 0; border: 0; border-bottom: 1px solid #d4dbcf; }
progressbar text { color: #304838; }
progressbar trough { min-height: 6px; border-radius: 4px; background: #dbe3d8; border: 0; }
progressbar progress { min-height: 6px; border-radius: 4px; background: #548466; border: 0; }
spinbutton, spinbutton entry { color: #25392c; background-color: #ffffff; }
checkbutton check { background: #ffffff; color: #284932; border: 1px solid #93a098; border-radius: 4px; }
checkbutton check:checked { background: #315c45; color: #ffffff; border-color: #315c45; }
checkbutton, expander title { color: #354c3b; }
stackswitcher button:checked { background: #315c45; border-color: #315c45; color: #ffffff; }
/* Popup menus are separate GTK surfaces and need their own theme rules. */
combobox, combobox button, combobox button:hover, combobox button:checked,
combobox button:active, combobox button:disabled, combobox cellview,
combobox cellview:selected, combobox cellview:disabled {
    background-image: none; background-color: #ffffff; color: #111111;
}
combobox button:disabled, combobox cellview:disabled { color: #596575; }
combobox arrow { color: #111111; }
menu, menuitem, menuitem label, .menu, .menuitem, popover, popover.background,
menuitem:hover, menuitem:selected, menuitem:checked,
menuitem:hover label, menuitem:selected label, menuitem:checked label {
    background-image: none; background-color: #ffffff; color: #111111;
    text-shadow: none;
}
menuitem:hover, menuitem:selected, menuitem:checked { box-shadow: inset 3px 0 #38785e; }
menuitem:disabled, menuitem:disabled label { background-color: #ffffff; color: #596575; }
popover modelbutton, popover modelbutton label, popover modelbutton:hover,
popover modelbutton:selected, popover modelbutton:checked {
    background-image: none; background-color: #ffffff; color: #111111;
}
"""
