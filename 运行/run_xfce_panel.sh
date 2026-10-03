#!/bin/bash
# 奇点OS 底部快捷启动栏（Dock）启动脚本
# 1) 面板配置不存在时写入默认 6 插件配置（XFCE 4.18 格式：type=value）
# 2) 动态继承会话环境后启动 xfce4-panel
set -e

PANEL_XML="${HOME}/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml
if [ ! -s "$PANEL_XML" ] || ! grep -q "plugin-ids" "$PANEL_XML" 2>/dev/null; then
  cat > "$PANEL_XML" <<'CFG'
<?xml version="1.0" encoding="UTF-8"?>

<channel name="xfce4-panel" version="1.0">
  <property name="configver" type="int" value="2"/>
  <property name="panels" type="array">
    <value type="int" value="1"/>
    <property name="panel-1" type="empty">
      <property name="position" type="string" value="p=6;x=640;y=760"/>
      <property name="size" type="uint" value="56"/>
      <property name="plugin-ids" type="array">
        <value type="int" value="1"/>
        <value type="int" value="2"/>
        <value type="int" value="3"/>
        <value type="int" value="4"/>
        <value type="int" value="5"/>
        <value type="int" value="6"/>
      </property>
    </property>
  </property>
  <property name="plugins" type="empty">
    <property name="plugin-1" type="string" value="launcher">
      <property name="items" type="array">
        <value type="string" value="奇点OS意图.desktop"/>
      </property>
    </property>
    <property name="plugin-2" type="string" value="clock">
      <property name="show-frame" type="bool" value="false"/>
    </property>
    <property name="plugin-3" type="string" value="launcher">
      <property name="items" type="array">
        <value type="string" value="奇点OS状态.desktop"/>
      </property>
    </property>
    <property name="plugin-4" type="string" value="launcher">
      <property name="items" type="array">
        <value type="string" value="xfce4-terminal.desktop"/>
      </property>
    </property>
    <property name="plugin-5" type="string" value="launcher">
      <property name="items" type="array">
        <value type="string" value="奇点OS文件浏览器.desktop"/>
      </property>
    </property>
    <property name="plugin-6" type="string" value="launcher">
      <property name="items" type="array">
        <value type="string" value="xfce-settings-manager.desktop"/>
      </property>
    </property>
  </property>
</channel>
CFG
  # 让 xfconf 重新读取
  pkill -x xfconfd 2>/dev/null || true
  sleep 1
fi

export DISPLAY=:0
export XAUTHORITY=/root/.Xauthority
export XDG_RUNTIME_DIR=/run/user/0
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/0/bus

# 从 xfwm4 进程动态继承 SESSION_MANAGER（xfce4-session 自身没有该变量）
XFWM_PID=$(pgrep -x xfwm4 | head -1)
if [ -n "$XFWM_PID" ] && [ -r "/proc/$XFWM_PID/environ" ]; then
  SM=$(tr '\0' '\n' < "/proc/$XFWM_PID/environ" | grep '^SESSION_MANAGER=' || true)
  [ -n "$SM" ] && export "$SM"
fi

# 清理旧面板实例后启动（避免重复）
pkill -x xfce4-panel 2>/dev/null || true
sleep 1
exec xfce4-panel
