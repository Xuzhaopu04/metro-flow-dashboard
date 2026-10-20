import pymysql

# 让 Django 用纯 Python 的 pymysql 作为 MySQL 驱动，
# 避免 Windows 上安装 mysqlclient 需要 C++ 编译环境。
pymysql.install_as_MySQLdb()
