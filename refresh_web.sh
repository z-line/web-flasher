#!/bin/bash

# 获取脚本实际所在目录的绝对路径（支持符号链接）
if command -v readlink >/dev/null 2>&1 && readlink -f "${BASH_SOURCE[0]}" >/dev/null 2>&1; then
    # Linux/Unix with readlink -f support
    current_absolute_path="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
else
    # Fallback for systems without readlink -f (e.g., macOS)
    script_path="${BASH_SOURCE[0]:-$0}"
    while [ -L "$script_path" ]; do
        dir="$(dirname "$script_path")"
        script_path="$(readlink "$script_path")"
        case "$script_path" in
            /*) ;;
            *) script_path="$dir/$script_path" ;;
        esac
    done
    current_absolute_path="$(cd "$(dirname "$script_path")" && pwd)"
fi

web_source_dir="$current_absolute_path"
targets_dir="$(dirname "$current_absolute_path")/ExpressLRSTargets"
echo "Script directory: $current_absolute_path"
echo "Targets directory: $targets_dir"

function rebuild_web() {
    echo "Rebuilding web assets..."
    cd "$web_source_dir" || exit
    npm install
    npm run build
    echo "Web assets rebuilt."
}

function check_elrs_firmware(){
    index_url="https://artifactory.expresslrs.org/ExpressLRS/index.json"
    index_path="$current_absolute_path/public/assets/firmware/index.json"
    # 检查 index.json 是否存在
    if [ ! -f "$index_path" ]; then
        echo "Firmware index.json not found."
        return 1
    fi
    # 获取最新的 index.json，并与本地进行比较
    temp_index="$(mktemp)"
    curl -s -L -o "$temp_index" "$index_url"
    if ! cmp -s "$temp_index" "$index_path"; then
        echo "New firmware version detected."
        rm "$temp_index"
        return 1
    else
        echo "Firmware is up to date."
        rm "$temp_index"
        return 0
    fi
}

function check_elrs_backpack(){
    index_url="https://artifactory.expresslrs.org/Backpack/index.json"
    index_path="$current_absolute_path/public/assets/backpack/index.json"
    # 检查 index.json 是否存在
    if [ ! -f "$index_path" ]; then
        echo "Backpack index.json not found."
        return 1
    fi
    # 获取最新的 index.json，并与本地进行比较
    temp_index="$(mktemp)"
    curl -s -L -o "$temp_index" "$index_url"
    if ! cmp -s "$temp_index" "$index_path"; then
        echo "New backpack firmware version detected."
        rm "$temp_index"
        return 1
    else
        echo "Backpack firmware is up to date."
        rm "$temp_index"
        return 0
    fi
}

function refresh_web_source() {
    echo "Refreshing web source files..."
    cd "$web_source_dir" || exit
    git_pull_output="$(git pull 2>&1)"
    git_status=$?
    echo "$git_pull_output"
    # 判断输出是否包含 "Already up to date." 或 "Already up-to-date."
    if echo "$git_pull_output" | grep -qF "Already up to date." || echo "$git_pull_output" | grep -qF "Already up-to-date."; then
        echo "Web source is already up to date."
        return 0
    elif [ $git_status -ne 0 ]; then
        echo "git pull failed with status $git_status"
        return 1
    else
        echo "Web source updated."
        return 1
    fi
    echo "Web source files refreshed."
}

function refresh_target_source(){
    echo "Refreshing target source files..."
    if ! [ -d "$targets_dir" ]; then
        echo "ExpressLRSTargets directory not found. Cloning repository..."
        git clone https://github.com/z-line/targets.git "$targets_dir"
    fi
    cd "$targets_dir" || exit
    git_pull_output="$(git pull 2>&1)"
    git_status=$?
    echo "$git_pull_output"
    if echo "$git_pull_output" | grep -qF "Already up to date." || echo "$git_pull_output" | grep -qF "Already up-to-date."; then
        echo "Target source is already up to date."
        return 0
    elif [ $git_status -ne 0 ]; then
        echo "git pull failed with status $git_status"
        return 1
    else
        echo "Target source updated."
        return 1
    fi
    echo "Target source files refreshed."
}

function soft_link_targets(){
    echo "Creating soft links for target source..."
    # 遍历"$web_source_dir/public/firmware"目录下的所有文件夹，将目录下hardware替换为ExpressLRSTargets的软链接
    firmware_assets_dir="$web_source_dir/public/assets/firmware"
    cd "$firmware_assets_dir" || exit
    for dir in */; do
        # 如果当前目录名为hardware则跳过
        if [ "$dir" == "hardware/" ]; then
            continue
        fi
        rm -rf "$dir/hardware"
        ln -s "$targets_dir" "$dir/hardware"
        echo "Linked hardware for $dir, $targets_dir -> $dir/hardware"
    done
    # 如果存在hardware目录，删除后创建软链接
    rm -rf hardware
    ln -s "$targets_dir" hardware
}

function deploy(){
    echo "Deploying web assets..."
    deploy_config_dir="$web_source_dir/deploy_config"
    dist_dir="$web_source_dir/dist"
    for config_file in "$deploy_config_dir"/*.json; do
        [ -e "$config_file" ] || continue
        config_filename=$(basename "$config_file")
        config_name="${config_filename%.json}"
        target_dir="$(dirname "$web_source_dir")/$config_name"
        if [ -d "$target_dir" ]; then
            rm -rf "$target_dir"
        fi
        mkdir -p "$target_dir"
        cp -r "$dist_dir/"* "$target_dir/"
        cp "$config_file" "$target_dir/config.json"
        echo "Deployed to $target_dir"
        
        # 创建ExpressLRSTargets软链接
        firmware_dir="$target_dir/assets/firmware"
        if [ -d "$firmware_dir" ]; then
            cd "$firmware_dir" || continue
            for dir in */; do
                # 如果当前目录名为hardware则跳过
                if [ "$dir" == "hardware/" ]; then
                    continue
                fi
                rm -rf "$dir/hardware"
                ln -s "$targets_dir" "$dir/hardware"
                echo "Linked hardware for $dir in $config_name"
            done
            # 如果存在hardware目录，删除后创建软链接
            rm -rf hardware
            ln -s "$targets_dir" hardware
            cd - > /dev/null || return
        fi
    done
    echo "Web assets deployed."
}

elrs_firmware_update=false
if ! check_elrs_firmware || ! check_elrs_backpack; then
    echo "Refreshing artifacts..."
    "$web_source_dir/get_artifacts.sh"
    elrs_firmware_update=true
else
    echo "No updates found. Skipping artifact refresh."
fi
soft_link_targets
source_update=false
if ! refresh_web_source || ! refresh_target_source; then
    source_update=true
fi
if [ "$elrs_firmware_update" = true ] || [ "$source_update" = true ]; then
    rebuild_web
    deploy
else
    echo "No changes detected. Skipping web rebuild."
fi

