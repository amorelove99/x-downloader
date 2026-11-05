#!/bin/zsh
# Resolve a Desktop symlink before locating project files.
launcher_path="${0:A}"
project_dir="${launcher_path:h}"
cd "$project_dir" || exit 1
export XDL_HOME="$project_dir"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
if [[ ! -x "$project_dir/.venv/bin/python" ]] || ! "$project_dir/.venv/bin/python" -c 'import rich, xdownload' >/dev/null 2>&1; then
  print '首次使用：将建立本项目 .venv 并安装 Python 依赖（Rich），不修改系统 Python。'
  print '1 安装并继续    0 退出'
  read 'install_choice?输入数字：'
  if [[ "$install_choice" != 1 ]]; then exit 0; fi
  mkdir -p "$project_dir/data" || exit 1
  install_log="$project_dir/data/install.log"
  if ! python3 -m venv "$project_dir/.venv" >>"$install_log" 2>&1; then
    print "建立虚拟环境失败，错误日志：$install_log"
    cat "$install_log"
    read 'pause_choice?按回车退出'
    exit 1
  fi
  print '正在安装项目依赖，请稍候……'
  if ! "$project_dir/.venv/bin/python" -m pip install -e "$project_dir" >>"$install_log" 2>&1; then
    print "安装失败；重开启动器可继续。日志：$install_log"
    cat "$install_log"
    read 'pause_choice?按回车退出'
    exit 1
  fi
  print '安装完成。下载设置中可创建桌面入口或 ~/.local/bin/xdl。'
fi
"$project_dir/.venv/bin/python" -m xdownload menu
exit_code=$?
if (( exit_code != 0 )); then
  print "程序已退出，退出码 $exit_code。"
  read 'pause_choice?按回车关闭'
fi
exit "$exit_code"
