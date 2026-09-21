# /// script
# dependencies = [
#   "beautifulsoup4",
#   "html2text",
# ]
# ///

import os
import re
from bs4 import BeautifulSoup
import html2text

def html2md(html_file):
    """将HTML文件转换为Markdown格式"""
    try:
        # 读取HTML文件
        with open(html_file, 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        # 使用BeautifulSoup解析HTML
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 移除script和style标签
        for script in soup(['script', 'style', 'meta', 'link']):
            script.decompose()
        
        # 移除头图和结尾图
        # 头图通常是article-content类下的第一个img标签
        # 结尾图通常是article-content类下的最后一个img标签
        article_content = soup.find('div', {'class': 'article-content'})
        if article_content:
            # 移除第一个img(头图)
            first_img = article_content.find('img')
            if first_img:
                first_img.decompose()
            
            # 移除最后一个img(结尾图)
            all_imgs = article_content.find_all('img')
            if all_imgs:
                all_imgs[-1].decompose()
        
        # 尝试找到主要内容区域
        # 极客时间的文章通常在特定的div中
        content = None
        
        # 尝试多种可能的内容选择器
        selectors = [
            {'class': 'article-content'},
            {'class': 'article'},
            {'id': 'article'},
            {'class': 'content'},
            {'class': 'main'},
        ]
        
        for selector in selectors:
            content = soup.find('div', selector)
            if content:
                break
        
        # 如果没找到特定区域,使用body
        if not content:
            content = soup.find('body')
        
        if not content:
            # 如果还是没找到,使用整个文档
            content = soup
        
        # 使用html2text转换为markdown
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = False
        h.ignore_emphasis = False
        h.body_width = 0  # 不限制行宽
        h.unicode_snob = True
        h.skip_internal_links = True
        
        # 转换为markdown
        markdown_text = h.handle(str(content))
        
        # 清理多余的空行
        markdown_text = re.sub(r'\n{3,}', '\n\n', markdown_text)
        
        return markdown_text
        
    except Exception as e:
        print(f'Error converting {html_file}: {str(e)}')
        return f'# 转换失败\n\n错误: {str(e)}'

def main():
    for file in os.listdir('HTML'):
        if file.endswith('.html'):
            file_path = os.path.join('HTML', file)
            md_file = os.path.join('MD', file.replace('.html', '.md'))
            os.makedirs(os.path.dirname(md_file), exist_ok=True)
            print(f'Converting {file_path} to {md_file}')
            with open(md_file, 'w') as f:
                f.write(html2md(file_path))
            print(f"Successfully converted {file_path} to {md_file}")

if __name__ == '__main__':
    main()