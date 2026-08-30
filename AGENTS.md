# 构建项目说明文档的skill

1. 先扫描当前项目真实目录和代码
2. 不依据旧 README 猜测当前实现
3. 核对：
   - backend/
   - frontend/
   - data/
   - scripts/
   - .github/workflows/
4. 实际读取入口文件、配置、依赖和运行脚本
5. 必要时运行 --help、语法检查或构建命令验证
6. 然后生成/更新：

README.md
docs/项目说明.md
docs/架构说明.md
docs/GITHUB_PAGES部署.md
docs/NAS部署.md
docs/数据格式.md
docs/故障排查.md
docs/CHANGELOG.md

7. 明确区分：
   - 当前已经实现
   - 当前限制
   - 计划功能
8. 禁止把“理论上应该如此”写成“当前已经实现”
9. 文档修改完成后重新对照代码审查一次